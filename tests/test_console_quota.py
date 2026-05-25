import unittest

from app.control.account.enums import QuotaSource
from app.control.account.models import AccountQuotaSet, AccountRecord, QuotaWindow
from app.control.account.quota_defaults import (
    default_quota_set,
    default_quota_window,
    supported_mode_ids,
    supports_mode,
)
from app.control.model.enums import ModeId
from app.control.model.registry import MODELS
from app.dataplane.account.sync import _record_to_slot_args
from app.dataplane.account.table import AccountRuntimeTable
from app.dataplane.shared.enums import PoolId


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


if __name__ == "__main__":
    unittest.main()
