"""Admin token CRUD — list, import, delete, replace pool.

Performance notes:
  - DI-injected repo (no try/except per call)
  - orjson direct output (bypasses stdlib json)
  - Quota dict: zero deserialization — reads r.quota directly
  - Import refresh: reuses app.state.refresh_service singleton
"""

import asyncio
import re
from typing import TYPE_CHECKING, Iterable

import orjson
from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, RootModel

from app.platform.errors import AppError, ErrorKind, ValidationError
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_ms
from app.control.account.commands import (
    AccountPatch,
    AccountUpsert,
    BulkReplacePoolCommand,
    ListAccountsQuery,
)
from app.control.account.enums import AccountStatus

if TYPE_CHECKING:
    from app.control.account.refresh import AccountRefreshService
    from app.control.account.repository import AccountRepository

from . import get_refresh_svc, get_repo

router = APIRouter(tags=["Admin - Tokens"])

_PAGE_SCAN_SIZE = 2000
_NON_INVALID_STATUSES = (
    AccountStatus.ACTIVE,
    AccountStatus.COOLING,
    AccountStatus.DISABLED,
)
_DEFAULT_POOLS = ("basic", "super", "heavy")

# ---------------------------------------------------------------------------
# Token sanitisation
# ---------------------------------------------------------------------------

_TOKEN_TRANS = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-",
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ",
    "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
})
_STRIP_RE = re.compile(r"\s+")


def _sanitize(value: str) -> str:
    tok = str(value or "").translate(_TOKEN_TRANS)
    tok = _STRIP_RE.sub("", tok)
    if tok.startswith("sso="):
        tok = tok[4:]
    return tok.encode("ascii", errors="ignore").decode("ascii")


def _mask(token: str) -> str:
    return f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ReplacePoolRequest(BaseModel):
    pool: str
    tokens: list[str]
    tags: list[str] = []


class AddTokensRequest(BaseModel):
    tokens: list[str]
    pool: str = "basic"
    tags: list[str] = []


class EditTokenRequest(BaseModel):
    old_token: str
    token: str
    pool: str = "basic"


class ToggleTokenDisabledRequest(BaseModel):
    token: str
    disabled: bool


class ToggleTokensDisabledRequest(BaseModel):
    tokens: list[str]
    disabled: bool


class TokenImportItem(BaseModel):
    token: str
    tags: list[str] = []


class SaveTokensRequest(RootModel[dict[str, list[str | TokenImportItem]]]):
    """Bulk-save payload keyed by pool name."""


# ---------------------------------------------------------------------------
# Serialisation — zero-copy quota extraction
# ---------------------------------------------------------------------------

def _quota_brief(q: dict) -> dict:
    """Extract per-mode remaining/total from stored quota dict."""
    out = {}
    for mode in ("auto", "fast", "expert", "heavy", "grok_4_3", "console"):
        v = q.get(mode)
        if isinstance(v, dict):
            brief = {
                "remaining": int(v.get("remaining", 0) or 0),
                "total": int(v.get("total", 0) or 0),
            }
            for key in ("window_seconds", "reset_at", "synced_at", "source"):
                if key in v:
                    brief[key] = v.get(key)
            out[mode] = brief
    return out


def _serialize_record(r) -> dict:
    return {
        "token":       r.token,
        "pool":        r.pool or "basic",
        "status":      r.status,
        "quota":       _quota_brief(r.quota) if isinstance(r.quota, dict) else {},
        "use_count":   r.usage_use_count or 0,
        "fail_count":  r.usage_fail_count or 0,
        "last_used_at": r.last_use_at,
        "tags":        r.tags or [],
    }


def _json(data) -> Response:
    """orjson fast-path response."""
    return Response(content=orjson.dumps(data), media_type="application/json")


def _parse_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _normalise_pool(pool: str | None) -> str | None:
    value = (pool or "").strip().lower()
    return None if value in ("", "all") else value


def _status_query(status: str | None) -> tuple[AccountStatus | None, list[AccountStatus]]:
    value = (status or "").strip().lower()
    if value in ("", "all"):
        return None, []
    if value == "invalid":
        return None, list(_NON_INVALID_STATUSES)
    try:
        return AccountStatus(value), []
    except ValueError as exc:
        raise ValidationError("Invalid status filter", param="status") from exc


def _build_list_query(
    *,
    page: int,
    page_size: int,
    pool: str | None,
    status: str | None,
    tags: str | None,
    exclude_tags: str | None,
    sort_by: str,
    sort_desc: bool,
) -> ListAccountsQuery:
    status_filter, status_not_in = _status_query(status)
    return ListAccountsQuery(
        page=page,
        page_size=page_size,
        pool=_normalise_pool(pool),
        status=status_filter,
        status_not_in=status_not_in,
        tags=_parse_csv(tags),
        exclude_tags=_parse_csv(exclude_tags),
        sort_by=sort_by,
        sort_desc=sort_desc,
    )


def _status_value(record) -> str:
    status = record.status
    return status.value if hasattr(status, "value") else str(status)


def _is_invalid_record(record) -> bool:
    return _status_value(record) not in {status.value for status in _NON_INVALID_STATUSES}


def _matches_pool(record, pool: str | None) -> bool:
    return pool is None or (record.pool or "basic") == pool


def _matches_status(record, status: str | None) -> bool:
    value = (status or "").strip().lower()
    if value in ("", "all"):
        return True
    if value == "invalid":
        return _is_invalid_record(record)
    _status_query(value)
    return _status_value(record) == value


def _matches_tags(record, tags: Iterable[str], exclude_tags: Iterable[str]) -> bool:
    record_tags = set(record.tags or [])
    return all(tag in record_tags for tag in tags) and not any(tag in record_tags for tag in exclude_tags)


def _empty_status_counts() -> dict[str, int]:
    return {"all": 0, "active": 0, "cooling": 0, "invalid": 0, "disabled": 0}


def _count_status(records: Iterable) -> dict[str, int]:
    counts = _empty_status_counts()
    for record in records:
        status = _status_value(record)
        counts["all"] += 1
        if status == AccountStatus.ACTIVE.value:
            counts["active"] += 1
        elif status == AccountStatus.COOLING.value:
            counts["cooling"] += 1
        elif status == AccountStatus.DISABLED.value:
            counts["disabled"] += 1
        else:
            counts["invalid"] += 1
    return counts


def _count_nsfw(records: Iterable) -> dict[str, int]:
    counts = {"all": 0, "enabled": 0, "disabled": 0}
    for record in records:
        counts["all"] += 1
        if "nsfw" in (record.tags or []):
            counts["enabled"] += 1
        else:
            counts["disabled"] += 1
    return counts


def _count_pools(records: Iterable) -> dict[str, int]:
    counts: dict[str, int] = {"all": 0}
    for record in records:
        pool = record.pool or "basic"
        counts["all"] += 1
        counts[pool] = counts.get(pool, 0) + 1
    return counts


def _summarise_records(records: list) -> dict:
    stats = {
        "total": 0,
        "active": 0,
        "cooling": 0,
        "invalid": 0,
        "disabled": 0,
        "calls": 0,
        "success": 0,
        "fail": 0,
        "qa": 0,
        "qf": 0,
        "qe": 0,
        "qh": 0,
        "qb": 0,
        "qc": 0,
        "by_pool": {},
        "nsfw": {"enabled": 0, "disabled": 0},
    }
    quota_keys = {
        "auto": "qa",
        "fast": "qf",
        "expert": "qe",
        "heavy": "qh",
        "grok_4_3": "qb",
        "console": "qc",
    }
    for record in records:
        status = _status_value(record)
        pool = record.pool or "basic"
        quota = _quota_brief(record.quota) if isinstance(record.quota, dict) else {}
        success = record.usage_use_count or 0
        fail = record.usage_fail_count or 0

        stats["total"] += 1
        stats["success"] += success
        stats["fail"] += fail
        stats["calls"] += success + fail
        stats["by_pool"][pool] = stats["by_pool"].get(pool, 0) + 1
        if "nsfw" in (record.tags or []):
            stats["nsfw"]["enabled"] += 1
        else:
            stats["nsfw"]["disabled"] += 1

        for mode, key in quota_keys.items():
            stats[key] += int(quota.get(mode, {}).get("remaining", 0) or 0)

        if status == AccountStatus.ACTIVE.value:
            stats["active"] += 1
        elif status == AccountStatus.COOLING.value:
            stats["cooling"] += 1
        elif status == AccountStatus.DISABLED.value:
            stats["disabled"] += 1
        else:
            stats["invalid"] += 1
    return stats


async def _all_live_records(repo: "AccountRepository") -> tuple[list, int]:
    records: list = []
    revision = 0
    page_num = 1
    while True:
        page = await repo.list_accounts(ListAccountsQuery(page=page_num, page_size=_PAGE_SCAN_SIZE))
        records.extend(page.items)
        revision = page.revision
        if page_num * _PAGE_SCAN_SIZE >= page.total:
            return records, revision
        page_num += 1


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/stats")
async def account_stats(
    pool: str | None = Query(default=None),
    status: str | None = Query(default=None),
    tags: str | None = Query(default=None),
    exclude_tags: str | None = Query(default=None),
    repo: "AccountRepository" = Depends(get_repo),
):
    """Return aggregate account stats without sending all token secrets."""
    pool_filter = _normalise_pool(pool)
    status_filter = (status or "").strip().lower()
    if status_filter:
        _status_query(status_filter)
    tag_filter = _parse_csv(tags)
    exclude_tag_filter = _parse_csv(exclude_tags)

    records, revision = await _all_live_records(repo)
    stats = _summarise_records(records)
    pools = sorted({*_DEFAULT_POOLS, *stats["by_pool"].keys()})

    status_records = [
        record for record in records
        if _matches_pool(record, pool_filter) and _matches_tags(record, tag_filter, exclude_tag_filter)
    ]
    nsfw_records = [
        record for record in records
        if _matches_pool(record, pool_filter) and _matches_status(record, status_filter)
    ]
    pool_records = [
        record for record in records
        if _matches_status(record, status_filter) and _matches_tags(record, tag_filter, exclude_tag_filter)
    ]

    pool_counts = _count_pools(pool_records)
    for pool_name in pools:
        pool_counts.setdefault(pool_name, 0)

    stats.update({
        "status_counts": _count_status(status_records),
        "nsfw_counts": _count_nsfw(nsfw_records),
        "pool_counts": pool_counts,
        "pools": pools,
        "revision": revision,
    })
    return _json(stats)


@router.get("/tokens")
async def list_tokens(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=2000),
    pool: str | None = Query(default=None),
    status: str | None = Query(default=None),
    tags: str | None = Query(default=None),
    exclude_tags: str | None = Query(default=None),
    sort_by: str = Query(default="updated_at"),
    sort_desc: bool = Query(default=True),
    repo: "AccountRepository" = Depends(get_repo),
):
    """Return one page of token records plus pagination metadata."""
    query = _build_list_query(
        page=page,
        page_size=page_size,
        pool=pool,
        status=status,
        tags=tags,
        exclude_tags=exclude_tags,
        sort_by=sort_by,
        sort_desc=sort_desc,
    )
    account_page = await repo.list_accounts(query)
    return _json({
        "tokens": [_serialize_record(r) for r in account_page.items],
        "total": account_page.total,
        "page": account_page.page,
        "page_size": account_page.page_size,
        "total_pages": account_page.total_pages,
        "revision": account_page.revision,
    })


@router.post("/tokens")
async def save_tokens(
    req: SaveTokensRequest,
    repo: "AccountRepository" = Depends(get_repo),
    refresh_svc: "AccountRefreshService" = Depends(get_refresh_svc),
):
    """Full pool replace — accepts {pool_name: [token_objects]} dict."""
    total_upserted = 0
    all_tokens: list[str] = []

    for pool_name, items in req.root.items():
        upserts = []
        for item in items:
            td = {"token": item} if isinstance(item, str) else item.model_dump()
            token_val = _sanitize(td.get("token", ""))
            if not token_val:
                continue
            upserts.append(AccountUpsert(token=token_val, pool=pool_name, tags=td.get("tags") or []))
        if upserts:
            await repo.replace_pool(BulkReplacePoolCommand(pool=pool_name, upserts=upserts))
            all_tokens.extend(u.token for u in upserts)
            total_upserted += len(upserts)

    logger.info("admin tokens saved across pools: saved_count={}", total_upserted)
    if all_tokens:
        asyncio.create_task(_refresh_imported(refresh_svc, all_tokens))
    return _json({"status": "success", "count": total_upserted})


@router.post("/tokens/add")
async def add_tokens(
    req: AddTokensRequest,
    repo: "AccountRepository" = Depends(get_repo),
    refresh_svc: "AccountRefreshService" = Depends(get_refresh_svc),
):
    requested_pool = (req.pool or "basic").strip().lower()
    sync_auto_detect = requested_pool == "auto"

    # Deduplicate and sanitize input
    cleaned: list[str] = []
    seen: set[str] = set()
    for token in req.tokens:
        tok = _sanitize(token)
        if tok and tok not in seen:
            seen.add(tok)
            cleaned.append(tok)
    if not cleaned:
        raise ValidationError("No valid tokens provided", param="tokens")

    # Only upsert tokens that are not already active — avoids overwriting quota/status.
    # Soft-deleted tokens are treated as non-existing so they can be restored.
    existing = {r.token for r in await repo.get_accounts(cleaned) if not r.is_deleted()}
    new_tokens = [t for t in cleaned if t not in existing]

    if not new_tokens:
        return _json({"status": "success", "count": 0, "skipped": len(cleaned)})

    upserts = [AccountUpsert(token=t, pool=requested_pool, tags=req.tags) for t in new_tokens]
    result = await repo.upsert_accounts(upserts)
    logger.info(
        "admin tokens added: pool={} added_count={} skipped_count={}",
        requested_pool,
        len(new_tokens),
        len(existing),
    )

    if sync_auto_detect:
        try:
            refresh_result = await refresh_svc.refresh_on_import(new_tokens)
            logger.info(
                "admin auto-detect quota sync completed: token_count={} refreshed={} failed={}",
                len(new_tokens), refresh_result.refreshed, refresh_result.failed,
            )
        except Exception as exc:
            logger.warning("admin auto-detect quota sync failed: token_count={} error={}", len(new_tokens), exc)
    else:
        asyncio.create_task(_refresh_imported(refresh_svc, new_tokens))

    return _json({
        "status": "success",
        "count": result.upserted or len(new_tokens),
        "skipped": len(existing),
        "synced": sync_auto_detect,
    })


@router.delete("/tokens")
async def delete_tokens(
    tokens: list[str] = Body(...),
    repo: "AccountRepository" = Depends(get_repo),
):
    cleaned = [t for t in (_sanitize(t) for t in tokens) if t]
    if not cleaned:
        raise ValidationError("No valid tokens provided", param="tokens")
    await repo.delete_accounts(cleaned)
    logger.info("admin tokens deleted: deleted_count={}", len(cleaned))
    return _json({"deleted": len(cleaned)})


@router.put("/tokens/edit")
async def edit_token(
    req: EditTokenRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    old_token = _sanitize(req.old_token)
    new_token = _sanitize(req.token)
    pool = (req.pool or "basic").strip().lower()

    if not old_token or not new_token:
        raise ValidationError("Token is required", param="token")

    records = await repo.get_accounts([old_token])
    if not records:
        raise AppError(
            "Account not found",
            kind=ErrorKind.VALIDATION,
            code="account_not_found",
            status=404,
        )
    record = records[0]

    if old_token != new_token:
        existing = await repo.get_accounts([new_token])
        if existing:
            raise AppError(
                "Target token already exists",
                kind=ErrorKind.VALIDATION,
                code="token_conflict",
                status=409,
            )

    await repo.upsert_accounts([AccountUpsert(
        token=new_token,
        pool=pool,
        tags=record.tags,
        ext=record.ext,
    )])

    if old_token == new_token:
        logger.info("admin token updated: token={} pool={}", _mask(new_token), pool)
        return _json({"status": "success", "token": new_token, "pool": pool})

    qs = record.quota_set()
    await repo.patch_accounts([AccountPatch(
        token=new_token,
        status=record.status,
        tags=record.tags,
        quota_auto=qs.auto.to_dict(),
        quota_fast=qs.fast.to_dict(),
        quota_expert=qs.expert.to_dict(),
        quota_heavy=qs.heavy.to_dict() if qs.heavy else None,
        quota_grok_4_3=qs.grok_4_3.to_dict() if qs.grok_4_3 else None,
        quota_console=qs.console.to_dict() if qs.console else None,
        usage_use_delta=record.usage_use_count,
        usage_fail_delta=record.usage_fail_count,
        usage_sync_delta=record.usage_sync_count,
        last_use_at=record.last_use_at,
        last_fail_at=record.last_fail_at,
        last_fail_reason=record.last_fail_reason,
        last_sync_at=record.last_sync_at,
        last_clear_at=record.last_clear_at,
        state_reason=record.state_reason,
        ext_merge=record.ext,
    )])
    await repo.delete_accounts([old_token])

    logger.info("admin token replaced: previous_token={} current_token={} pool={}", _mask(old_token), _mask(new_token), pool)
    return _json({"status": "success", "token": new_token, "pool": pool})


@router.post("/tokens/disabled")
async def toggle_token_disabled(
    req: ToggleTokenDisabledRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    token = _sanitize(req.token)
    if not token:
        raise ValidationError("Token is required", param="token")

    records = await repo.get_accounts([token])
    if not records:
        raise AppError(
            "Account not found",
            kind=ErrorKind.VALIDATION,
            code="account_not_found",
            status=404,
        )
    record = records[0]

    if req.disabled:
        await repo.patch_accounts([AccountPatch(
            token=token,
            status=AccountStatus.DISABLED,
            state_reason="operator_disabled",
            ext_merge={
                **record.ext,
                "disabled_at": now_ms(),
                "disabled_reason": "operator_disabled",
            },
        )])
        logger.info("admin token disabled: token={}", _mask(token))
        return _json({"status": "success", "token": token, "disabled": True})

    await repo.patch_accounts([AccountPatch(
        token=token,
        status=AccountStatus.ACTIVE,
        clear_failures=True,
    )])
    logger.info("admin token restored: token={}", _mask(token))
    return _json({"status": "success", "token": token, "disabled": False})


@router.post("/tokens/disabled/batch")
async def toggle_tokens_disabled(
    req: ToggleTokensDisabledRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in req.tokens:
        token = _sanitize(raw)
        if token and token not in seen:
            seen.add(token)
            cleaned.append(token)
    if not cleaned:
        raise ValidationError("No valid tokens provided", param="tokens")

    records = await repo.get_accounts(cleaned)
    if not records:
        raise AppError(
            "No matching accounts found",
            kind=ErrorKind.VALIDATION,
            code="account_not_found",
            status=404,
        )

    ts = now_ms()
    patches: list[AccountPatch] = []
    for record in records:
        if req.disabled:
            patches.append(AccountPatch(
                token=record.token,
                status=AccountStatus.DISABLED,
                state_reason="operator_disabled",
                ext_merge={
                    **record.ext,
                    "disabled_at": ts,
                    "disabled_reason": "operator_disabled",
                },
            ))
        else:
            patches.append(AccountPatch(
                token=record.token,
                status=AccountStatus.ACTIVE,
                clear_failures=True,
            ))

    result = await repo.patch_accounts(patches)
    logger.info(
        "admin tokens disabled batch updated: disabled={} requested_count={} patched_count={}",
        req.disabled,
        len(cleaned),
        result.patched,
    )
    return _json({
        "status": "success",
        "disabled": req.disabled,
        "summary": {
            "total": len(cleaned),
            "ok": result.patched,
            "fail": max(0, len(cleaned) - result.patched),
        },
    })


@router.put("/tokens/pool")
async def replace_pool(
    req: ReplacePoolRequest,
    repo: "AccountRepository" = Depends(get_repo),
    refresh_svc: "AccountRefreshService" = Depends(get_refresh_svc),
):
    cleaned = [t for t in (_sanitize(t) for t in req.tokens) if t]
    upserts = [AccountUpsert(token=t, pool=req.pool, tags=req.tags) for t in cleaned]
    await repo.replace_pool(BulkReplacePoolCommand(pool=req.pool, upserts=upserts))
    logger.info("admin pool replaced: pool={} token_count={}", req.pool, len(cleaned))
    if cleaned:
        asyncio.create_task(_refresh_imported(refresh_svc, cleaned))
    return _json({"pool": req.pool, "count": len(cleaned)})


# ---------------------------------------------------------------------------
# Fire-and-forget import refresh
# ---------------------------------------------------------------------------

async def _refresh_imported(svc: "AccountRefreshService", tokens: list[str]) -> None:
    try:
        await svc.refresh_on_import(tokens)
        logger.info("admin import quota sync completed: token_count={}", len(tokens))
    except Exception as exc:
        logger.warning("admin import quota sync failed: token_count={} error={}", len(tokens), exc)
