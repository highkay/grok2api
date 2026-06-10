import unittest
from unittest.mock import patch

from app.control.account.commands import AccountPatch
from app.control.account.enums import QuotaSource
from app.control.account.models import (
    AccountQuotaSet,
    AccountRecord,
    QuotaWindow,
    RuntimeSnapshot,
)
from app.control.account.quota_defaults import (
    default_quota_set,
    default_quota_window,
    supported_mode_ids,
    supports_mode,
)
from app.control.account.refresh import AccountRefreshService
from app.control.model.enums import ModeId
from app.control.model.registry import MODELS
from app.dataplane.account.sync import _record_to_slot_args
from app.dataplane.account.table import AccountRuntimeTable
from app.dataplane.shared.enums import PoolId


_MODE_PATCHES = {
    0: "quota_auto",
    1: "quota_fast",
    2: "quota_expert",
    3: "quota_heavy",
    4: "quota_grok_4_3",
    5: "quota_console",
}


class _MemoryAccountRepository:
    def __init__(self, records: list[AccountRecord]):
        self.records = {record.token: record for record in records}
        self.patches: list[AccountPatch] = []

    async def get_accounts(self, tokens: list[str]) -> list[AccountRecord]:
        return [self.records[token] for token in tokens if token in self.records]

    async def runtime_snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(items=list(self.records.values()), revision=0)

    async def patch_accounts(self, patches: list[AccountPatch]):
        self.patches.extend(patches)
        for account_patch in patches:
            record = self.records[account_patch.token]
            quota_set = record.quota_set()
            updates = {}
            if account_patch.pool is not None:
                updates["pool"] = account_patch.pool
            for mode_id, field in _MODE_PATCHES.items():
                value = getattr(account_patch, field)
                if value is not None:
                    quota_set.set(mode_id, QuotaWindow.from_dict(value))
            updates["quota"] = quota_set.to_dict()
            if account_patch.usage_use_delta is not None:
                updates["usage_use_count"] = (
                    record.usage_use_count + account_patch.usage_use_delta
                )
            if account_patch.usage_sync_delta is not None:
                updates["usage_sync_count"] = (
                    record.usage_sync_count + account_patch.usage_sync_delta
                )
            if account_patch.last_use_at is not None:
                updates["last_use_at"] = account_patch.last_use_at
            if account_patch.last_sync_at is not None:
                updates["last_sync_at"] = account_patch.last_sync_at
            self.records[account_patch.token] = record.model_copy(update=updates)
        return None


class ConsoleQuotaTests(unittest.TestCase):
    def test_console_models_use_independent_mode(self):
        console = [m for m in MODELS if m.is_console()]
        self.assertTrue(console)
        self.assertTrue(all(m.mode_id == ModeId.CONSOLE for m in console))

    def test_console_default_bucket_supported_for_all_pools(self):
        self.assertEqual(supported_mode_ids("basic"), (1, 5))
        for pool in ("basic", "super", "heavy"):
            self.assertTrue(supports_mode(pool, int(ModeId.CONSOLE)))
            window = default_quota_window(pool, int(ModeId.CONSOLE))
            self.assertIsNotNone(window)
            self.assertEqual(
                (window.remaining, window.total, window.window_seconds),
                (30, 30, 900),
            )

    def test_account_quota_set_serializes_console(self):
        quota_set = default_quota_set("basic")
        self.assertIsNotNone(quota_set.console)
        self.assertEqual(quota_set.get(int(ModeId.CONSOLE)), quota_set.console)
        updated = QuotaWindow(
            remaining=7,
            total=30,
            window_seconds=900,
            reset_at=None,
            synced_at=None,
            source=QuotaSource.ESTIMATED,
        )
        quota_set.set(int(ModeId.CONSOLE), updated)
        encoded = quota_set.to_dict()
        self.assertEqual(encoded["console"]["remaining"], 7)
        decoded = AccountQuotaSet.from_dict(encoded)
        self.assertEqual(decoded.get(int(ModeId.CONSOLE)).remaining, 7)

    def test_dataplane_slot_args_include_console_bucket(self):
        record = AccountRecord(
            token="abc",
            pool="basic",
            quota=default_quota_set("basic").to_dict(),
        )
        args = _record_to_slot_args(record)
        self.assertEqual(args["quota_console"], 30)
        self.assertEqual(args["total_console"], 30)
        self.assertEqual(args["window_console"], 900)

        tags = args.pop("tags")
        table = AccountRuntimeTable()
        idx = table._append_slot(record.token, **args, tags=tags)
        self.assertEqual(table.quota_for(idx, int(ModeId.CONSOLE)), 30)
        self.assertIn(
            idx,
            table.mode_available[(int(PoolId.BASIC), int(ModeId.CONSOLE))],
        )


class ConsoleQuotaRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_refresh_probes_tier_modes_without_console(self):
        record = AccountRecord(
            token="tok-heavy",
            pool="basic",
            quota=default_quota_set("basic").to_dict(),
        )
        repo = _MemoryAccountRepository([record])
        svc = AccountRefreshService(repo)
        captured_mode_ids: list[int] = []

        async def fake_fetch_all_quotas(token, mode_ids):
            self.assertEqual(token, "tok-heavy")
            captured_mode_ids.extend(mode_ids)
            return {
                0: QuotaWindow(150, 150, 7200, None, 1000, QuotaSource.REAL),
                1: QuotaWindow(400, 400, 7200, None, 1000, QuotaSource.REAL),
                2: QuotaWindow(150, 150, 7200, None, 1000, QuotaSource.REAL),
                3: QuotaWindow(20, 20, 7200, None, 1000, QuotaSource.REAL),
                4: QuotaWindow(150, 150, 7200, None, 1000, QuotaSource.REAL),
            }

        with patch(
            "app.dataplane.reverse.protocol.xai_usage.fetch_all_quotas",
            fake_fetch_all_quotas,
        ):
            result = await svc.refresh_tokens(["tok-heavy"])

        updated = repo.records["tok-heavy"]
        quota_set = updated.quota_set()
        self.assertEqual(result.refreshed, 1)
        self.assertEqual(updated.pool, "heavy")
        self.assertEqual(set(captured_mode_ids), {0, 1, 2, 3, 4})
        self.assertNotIn(int(ModeId.CONSOLE), captured_mode_ids)
        self.assertEqual(quota_set.heavy.total, 20)
        self.assertEqual(quota_set.grok_4_3.total, 150)

    async def test_console_local_use_starts_reset_timer_at_threshold(self):
        now = 1_700_000_000_000
        quota_set = default_quota_set("basic")
        quota_set.set(
            int(ModeId.CONSOLE),
            QuotaWindow(16, 30, 900, None, None, QuotaSource.ESTIMATED),
        )
        record = AccountRecord(
            token="tok-console",
            pool="basic",
            quota=quota_set.to_dict(),
        )
        repo = _MemoryAccountRepository([record])
        svc = AccountRefreshService(repo)

        await svc._apply_single_mode(
            record,
            int(ModeId.CONSOLE),
            None,
            is_use=True,
            use_at_ms=now,
        )

        updated = repo.records["tok-console"]
        console = updated.quota_set().console
        self.assertEqual(console.remaining, 15)
        self.assertEqual(console.reset_at, now + 900_000)
        self.assertEqual(updated.usage_use_count, 1)
        self.assertEqual(updated.last_use_at, now)

    async def test_console_local_use_above_threshold_keeps_reset_unknown(self):
        now = 1_700_000_000_000
        record = AccountRecord(
            token="tok-console",
            pool="basic",
            quota=default_quota_set("basic").to_dict(),
        )
        repo = _MemoryAccountRepository([record])
        svc = AccountRefreshService(repo)

        await svc._apply_single_mode(
            record,
            int(ModeId.CONSOLE),
            None,
            is_use=True,
            use_at_ms=now,
        )

        console = repo.records["tok-console"].quota_set().console
        self.assertEqual(console.remaining, 29)
        self.assertIsNone(console.reset_at)

    async def test_reset_expired_console_windows_restores_default_bucket(self):
        now = 1_700_000_000_000
        quota_set = default_quota_set("basic")
        quota_set.set(
            int(ModeId.CONSOLE),
            QuotaWindow(0, 30, 900, now - 1, None, QuotaSource.ESTIMATED),
        )
        record = AccountRecord(
            token="tok-console",
            pool="basic",
            quota=quota_set.to_dict(),
        )
        repo = _MemoryAccountRepository([record])
        svc = AccountRefreshService(repo)

        with patch("app.control.account.refresh.now_ms", return_value=now):
            reset_count = await svc.reset_expired_console_windows()

        console = repo.records["tok-console"].quota_set().console
        self.assertEqual(reset_count, 1)
        self.assertEqual(console.remaining, 30)
        self.assertEqual(console.total, 30)
        self.assertIsNone(console.reset_at)
        self.assertEqual(console.source, QuotaSource.DEFAULT)


if __name__ == "__main__":
    unittest.main()
