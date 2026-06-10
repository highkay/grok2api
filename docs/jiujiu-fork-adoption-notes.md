# jiujiu532/grok2api fork adoption notes

Date: 2026-06-10

Source fork: `https://github.com/jiujiu532/grok2api`

## Adopted

- Bootstrap quota probing on account import and manual refresh now probes the upstream usage modes `auto`, `fast`, `expert`, `heavy`, and `grok_4_3`. The returned live windows are used to correct the local account pool to `basic`, `super`, or `heavy`.
- The bootstrap probe intentionally excludes `console`, because console quota is a local-only bucket and does not have a matching upstream usage API window.
- Console quota local decrement now starts its reset timer only after the local bucket reaches half capacity or lower. The scheduler resets expired console windows back to the default `30` requests / `15` minutes bucket.
- `account.refresh.console_reset_interval_sec` controls the console reset scan interval. The default is `30` seconds.

## Deferred

- Broad model alias and Statsig changes were not merged. They touch wider request routing and compatibility behavior, so they need separate endpoint-level verification before adoption.
- Fork-only UI reshaping was not merged. This branch keeps the existing admin UI and only exposes the new console reset interval in the current config schema.

## Verification

- `uv run ruff check --fix app/control/account/refresh.py app/control/account/scheduler.py tests/test_console_quota.py`
- `$env:PYTHONPATH='.'; uv run --frozen --with pytest --with pytest-asyncio python -m pytest tests`
