import orjson
import pytest

from app.control.account.backends.local import LocalAccountRepository
from app.control.account.commands import AccountPatch, AccountUpsert, ListAccountsQuery
from app.control.account.enums import AccountStatus
from app.products.web.admin import tokens as admin_tokens


async def _seed_repo(tmp_path):
    repo = LocalAccountRepository(tmp_path / "accounts.db")
    await repo.initialize()
    await repo.upsert_accounts([
        AccountUpsert(token="tok-active-basic-nsfw", pool="basic", tags=["nsfw"]),
        AccountUpsert(token="tok-active-basic", pool="basic"),
        AccountUpsert(token="tok-cooling-super-nsfw", pool="super", tags=["nsfw"]),
        AccountUpsert(token="tok-expired-super", pool="super"),
        AccountUpsert(token="tok-disabled-heavy", pool="heavy"),
    ])
    await repo.patch_accounts([
        AccountPatch(
            token="tok-active-basic-nsfw",
            status=AccountStatus.ACTIVE,
            quota_console={
                "remaining": 7,
                "total": 30,
                "window_seconds": 900,
                "reset_at": 123456,
                "synced_at": 120000,
                "source": 2,
            },
            usage_use_delta=3,
            usage_fail_delta=1,
        ),
        AccountPatch(
            token="tok-active-basic",
            status=AccountStatus.ACTIVE,
            usage_use_delta=2,
        ),
        AccountPatch(
            token="tok-cooling-super-nsfw",
            status=AccountStatus.COOLING,
            usage_fail_delta=4,
        ),
        AccountPatch(token="tok-expired-super", status=AccountStatus.EXPIRED),
        AccountPatch(token="tok-disabled-heavy", status=AccountStatus.DISABLED),
    ])
    return repo


@pytest.mark.asyncio
async def test_account_repository_supports_invalid_and_tag_filters(tmp_path):
    repo = await _seed_repo(tmp_path)

    invalid = await repo.list_accounts(ListAccountsQuery(
        page=1,
        page_size=10,
        status_not_in=[
            AccountStatus.ACTIVE,
            AccountStatus.COOLING,
            AccountStatus.DISABLED,
        ],
        sort_by="token",
        sort_desc=False,
    ))
    assert [record.token for record in invalid.items] == ["tok-expired-super"]

    nsfw = await repo.list_accounts(ListAccountsQuery(
        page=1,
        page_size=10,
        tags=["nsfw"],
        sort_by="token",
        sort_desc=False,
    ))
    assert [record.token for record in nsfw.items] == [
        "tok-active-basic-nsfw",
        "tok-cooling-super-nsfw",
    ]

    non_nsfw = await repo.list_accounts(ListAccountsQuery(
        page=1,
        page_size=10,
        exclude_tags=["nsfw"],
        sort_by="token",
        sort_desc=False,
    ))
    assert [record.token for record in non_nsfw.items] == [
        "tok-active-basic",
        "tok-disabled-heavy",
        "tok-expired-super",
    ]


@pytest.mark.asyncio
async def test_admin_tokens_endpoint_paginates_and_keeps_status_semantics(tmp_path):
    repo = await _seed_repo(tmp_path)

    invalid = orjson.loads((await admin_tokens.list_tokens(
        page=1,
        page_size=2,
        pool=None,
        status="invalid",
        tags=None,
        exclude_tags=None,
        sort_by="token",
        sort_desc=False,
        repo=repo,
    )).body)
    assert invalid["total"] == 1
    assert invalid["total_pages"] == 1
    assert invalid["tokens"][0]["token"] == "tok-expired-super"
    assert invalid["tokens"][0]["status"] == "expired"

    disabled = orjson.loads((await admin_tokens.list_tokens(
        page=1,
        page_size=2,
        pool=None,
        status="disabled",
        tags=None,
        exclude_tags=None,
        sort_by="token",
        sort_desc=False,
        repo=repo,
    )).body)
    assert disabled["total"] == 1
    assert disabled["tokens"][0]["token"] == "tok-disabled-heavy"

    nsfw_page = orjson.loads((await admin_tokens.list_tokens(
        page=1,
        page_size=1,
        pool=None,
        status=None,
        tags="nsfw",
        exclude_tags=None,
        sort_by="token",
        sort_desc=False,
        repo=repo,
    )).body)
    assert nsfw_page["total"] == 2
    assert nsfw_page["total_pages"] == 2
    assert nsfw_page["tokens"][0]["token"] == "tok-active-basic-nsfw"
    assert nsfw_page["tokens"][0]["fail_count"] == 1
    assert nsfw_page["tokens"][0]["quota"]["console"] == {
        "remaining": 7,
        "total": 30,
        "window_seconds": 900,
        "reset_at": 123456,
        "synced_at": 120000,
        "source": 2,
    }


@pytest.mark.asyncio
async def test_admin_stats_aggregates_without_token_secrets(tmp_path):
    repo = await _seed_repo(tmp_path)

    stats = orjson.loads((await admin_tokens.account_stats(
        pool=None,
        status=None,
        tags=None,
        exclude_tags=None,
        repo=repo,
    )).body)
    assert stats["total"] == 5
    assert stats["active"] == 2
    assert stats["cooling"] == 1
    assert stats["invalid"] == 1
    assert stats["disabled"] == 1
    assert stats["success"] == 5
    assert stats["fail"] == 5
    assert stats["calls"] == 10
    assert stats["status_counts"] == {
        "all": 5,
        "active": 2,
        "cooling": 1,
        "invalid": 1,
        "disabled": 1,
    }
    assert stats["nsfw_counts"] == {"all": 5, "enabled": 2, "disabled": 3}
    assert stats["pool_counts"] == {
        "all": 5,
        "basic": 2,
        "heavy": 1,
        "super": 2,
    }
    assert "tokens" not in stats
    assert "tok-active-basic-nsfw" not in orjson.dumps(stats).decode()

    filtered = orjson.loads((await admin_tokens.account_stats(
        pool="basic",
        status="all",
        tags="nsfw",
        exclude_tags=None,
        repo=repo,
    )).body)
    assert filtered["status_counts"] == {
        "all": 1,
        "active": 1,
        "cooling": 0,
        "invalid": 0,
        "disabled": 0,
    }
    assert filtered["nsfw_counts"] == {"all": 2, "enabled": 1, "disabled": 1}
    assert filtered["pool_counts"]["all"] == 2
    assert filtered["pool_counts"]["basic"] == 1
    assert filtered["pool_counts"]["super"] == 1
