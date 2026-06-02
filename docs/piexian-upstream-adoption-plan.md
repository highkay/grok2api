# piexian/grok2api 上游功能吸收方案

## 结论

`piexian/main` 是一个值得持续跟踪的活跃上游，但不适合直接整体合并到当前 `main`。

推荐策略是：保留 `piexian` remote，按功能域分阶段吸收高价值改动，并把本地已经验证过的安全、代理分流和 console quota 回归测试作为硬边界。任何阶段都不应以删除本地测试或回退本地修复为代价。

## 已验证事实

- 已添加 remote：`piexian https://github.com/piexian/grok2api.git`。
- 实施前基线工作区干净：`main...origin/main`。
- `piexian/main` 相对 `chenyme/main` 是纯前进分支：`0 / 16`。
- `piexian/main` 相对本地 `main` 已分叉：本地独有 10 个提交，piexian 独有 13 个提交。
- `git merge-tree` 显示 22 个 `changed in both` 文件，覆盖账号后端、模型注册、OpenAI 路由、Admin 页面、i18n、`pyproject.toml` 和 `uv.lock`。
- `git diff --check main..piexian/main` 发现 `app/dataplane/reverse/protocol/xai_console.py` 存在 trailing whitespace。
- 直接合并会删除本地关键回归测试：
  - `tests/test_asset_upload_security.py`
  - `tests/test_console_quota.py`
  - `tests/test_openai_chat_console_route.py`
  - `tests/test_proxy_host_routing.py`
  - `tests/test_xai_console_protocol.py`
- 直接合并会回退本地关键修复：
  - `app/dataplane/reverse/transport/asset_upload.py` 会重新对用户传入的外部 URL 请求使用 Grok 认证头。
  - `app/control/proxy/__init__.py` 会移除按目标 host 分流代理的逻辑。
  - `config.defaults.toml` 会移除 `proxy.egress.proxy_hosts` 和 `proxy.egress.direct_hosts`。
  - `.github/workflows/docker.yml` 会移除 `workflow_dispatch` 和 sha tag。

## 目标

1. 吸收上游高价值功能：Admin 分页、账号订阅识别、`account_id` 去重、`lite` pool、hybrid 模型默认 reasoning effort。
2. 保留本地已经修复并验证过的安全与代理边界：资源 URL 不泄露 Grok 认证头、console 与 grok.com 按目标 host 分流、console quota 独立桶。
3. 每个阶段都能独立验证、独立回滚，不把多个高风险变更绑成一个大合并。
4. 形成可长期复用的上游吸收流程，而不是一次性手工合并。

## 非目标

- 不直接 merge `piexian/main`。
- 不引入 piexian 的发布元数据、镜像归属或 README 品牌内容。
- 不删除本地现有回归测试。
- 不改动生产配置或数据，直到对应阶段的本地测试和迁移读回脚本完成。

## 总体执行方式

新建独立工作分支：

```powershell
git fetch piexian main
git switch -c feature/piexian-upstream-adoption main
```

每个阶段单独提交。阶段之间必须运行本阶段测试和本地已有保护测试。若某阶段失败，只回滚该阶段提交，不影响其他已完成阶段。

## 阶段 0：建立保护网

目的：先把“不允许回退”的本地行为固定住，防止上游吸收时悄悄覆盖。

必须保留并运行：

```powershell
uv run pytest tests/test_asset_upload_security.py
uv run pytest tests/test_proxy_host_routing.py
uv run pytest tests/test_console_quota.py
uv run pytest tests/test_openai_chat_console_route.py
uv run pytest tests/test_xai_console_protocol.py
```

如果本地环境缺依赖，先运行：

```powershell
uv sync
```

验收标准：

- 资源 URL 下载请求不携带 Grok token、cookie 或 x.ai 用户身份头。
- `proxy.egress.proxy_hosts/direct_hosts` 继续生效。
- console clearance 使用 `console.x.ai` 对应 origin。
- console quota 仍有独立桶，Admin 和路由侧行为不倒退。

## 阶段 1：吸收 Admin 服务端分页和统计

价值：高。

适用场景：账号数量变大后，Admin 账号页一次性加载全部 token 会变慢，分页能降低页面首屏和交互成本。

参考上游提交：

- `8b0f5ba feat(admin): server-side pagination + in-memory stats for account panel`
- `478b8f6 feat(account): merge upstream lite pool tier + fix admin pagination P2 regressions` 中与分页修正相关的部分

建议吸收范围：

- `app/products/web/admin/tokens.py`
  - 增加 `page`、`page_size`、`pool`、`status`、`tags`、`exclude_tags`、`sort_by`、`sort_desc` 查询参数。
  - 返回 `tokens`、`total`、`page`、`page_size`、`total_pages`、`revision`。
- `app/control/account/commands.py`
  - 扩展 `ListAccountsQuery`，支持分页、过滤和排序。
- `app/control/account/backends/local.py`
- `app/control/account/backends/sql.py`
- `app/control/account/backends/redis.py`
  - 实现分页查询，不改变现有账户状态语义。
- `app/statics/admin/account.html`
  - 账号列表改为分页拉取。
  - 保留本地 console quota 展示逻辑。

不要吸收：

- 会覆盖本地 console quota bucket 的 UI 改动。
- 与发布版本、README、镜像归属相关的内容。

新增或调整测试：

```powershell
uv run pytest tests/test_admin_tokens_pagination.py
uv run pytest tests/test_console_quota.py
```

建议新增 `tests/test_admin_tokens_pagination.py`，覆盖：

- 默认第一页返回正确 `page/page_size/total/total_pages`。
- `page_size` 上限不超过 2000，匹配当前 Admin 页面可选分页大小和导出批量拉取大小。
- pool/status/tags 过滤结果和 total 一致。
- 排序稳定。
- 空页返回空列表但 metadata 正确。

验收标准：

- 账号页可分页加载。
- Admin API 不再默认返回全量账号。
- console quota pill、禁用状态、现有筛选行为不回退。
- 本地保护测试全部通过。

## 阶段 2：吸收 `/rest/subscriptions` 订阅识别和 `account_id` 去重

价值：高，但风险最高。

适用场景：

- 多个 cookie 实际属于同一个 xAI 账号，导入后重复占用池。
- 账号池等级需要由真实订阅信息判定，而不是只靠导入时人工指定。
- 需要把失效 token 和重复 token 更可靠地清出去。

参考上游提交：

- `6438d85 feat(account): account_id dedup + Grok subscription API`
- `510c0e6 fix(account): super tier must not fall back to lite; cookie script cleanups`

建议吸收范围：

- 新增 `app/control/account/xai_subscription.py`
  - 请求 `https://grok.com/rest/subscriptions`。
  - 解析 `xaiUserId` 为 `account_id`。
  - 根据 active subscription 推导 pool。
- `app/control/account/models.py`
  - `AccountRecord` 增加 `account_id: str | None`。
  - 同步修正文档注释，pool 必须包含 `basic/lite/super/heavy`。
- `app/control/account/backends/local.py`
- `app/control/account/backends/sql.py`
- `app/control/account/backends/redis.py`
  - 增加 `account_id` 持久化。
  - 同一 `account_id` 只保留一个 active token，其余软删除。
- `app/control/account/refresh.py`
  - 导入和刷新时补全 `account_id` 和 pool。
  - 401/403 明确凭证失效时禁用账号。
- `app/products/web/admin/tokens.py`
- `app/statics/admin/account.html`
  - Admin 返回并显示 `account_id`，方便排查重复账号。

必须做的本地修正：

- `xai_subscription.py` 获取代理 lease 时要确认是否需要传入 `clearance_origin`。订阅接口在 `grok.com`，不能误用 console origin。
- 订阅接口失败不能误删账号：
  - 401/403 可以按 invalid credentials 处理。
  - timeout、5xx、代理失败只能记录失败或跳过，不应禁用账号。
- 去重必须只软删除明确同 `account_id` 的重复项。
- 软删除前需要保留审计信息：被保留 token、被删除 token、account_id、原因、时间。

新增测试：

```powershell
uv run pytest tests/test_xai_subscription.py
uv run pytest tests/test_account_id_dedup.py
uv run pytest tests/test_asset_upload_security.py
uv run pytest tests/test_proxy_host_routing.py
```

建议 `tests/test_xai_subscription.py` 覆盖：

- active `SUPER_GROK_PRO` -> `super` 或 `heavy`，按实际映射确认。
- active `SUPER_GROK_LITE` -> `lite`。
- inactive/canceled subscription 不提升 pool。
- 多个 subscription 时选择最高 active tier。
- unknown tier 不误判为高级账号。
- timeout/5xx 返回不可用但不禁用账号。
- 401/403 进入 invalid credentials 流程。

建议 `tests/test_account_id_dedup.py` 覆盖：

- local/sql/redis 三种后端同 `account_id` 导入重复 token，只保留一个 active。
- 不同 `account_id` 不互相影响。
- `account_id` 为空时不做去重。
- 软删除账号不进入可用池。
- 去重后 revision 更新，Admin total 与列表一致。

数据安全要求：

- 在生产导入该阶段前，先备份账号存储。
- 对 SQLite：备份 `data/*.db`。
- 对 Redis/Postgres/MySQL：先导出 token、status、pool、account_id、deleted_at。
- 首次上线只运行 subscription refresh，不立即大规模删除；先 dry-run 输出重复组。

验收标准：

- 真实或模拟订阅响应能正确推导 `account_id` 和 pool。
- 重复账号能被识别并软删除。
- 网络失败不会误伤账号。
- 三种后端行为一致。
- 本地安全与代理保护测试继续通过。

## 阶段 3：吸收 `lite` pool

价值：中到高，取决于实际账号池中是否存在 `super_grok_lite`。

参考上游提交：

- `478b8f6 feat(account): merge upstream lite pool tier + fix admin pagination P2 regressions`
- `510c0e6 fix(account): super tier must not fall back to lite`

建议吸收范围：

- `app/control/model/enums.py`
- `app/control/model/spec.py`
- `app/dataplane/shared/enums.py`
- `app/control/account/models.py`
- `app/dataplane/account/table.py`
- Admin 页面 pool 展示和筛选。

关键语义：

- `basic` 可以作为最低等级。
- `lite` 高于 `basic`，低于 `super`。
- `super` 不应回退到 `lite`。
- `heavy` 仍是最高等级。

新增测试：

```powershell
uv run pytest tests/test_account_pool_tiers.py
```

建议覆盖：

- `lite` 模型只能选择 `lite/super/heavy`，不能选择 `basic`。
- `super` 模型不能选择 `lite`。
- `heavy` 模型只能选择 `heavy`。
- Admin pool 过滤能识别 `lite`。

验收标准：

- pool 选择顺序符合业务语义。
- 没有把 `super` 降级到 `lite` 的隐性 fallback。
- 账号页、API 返回、持久化后端都能处理 `lite`。

## 阶段 4：吸收 hybrid 模型默认 `reasoning_effort=high`

价值：中。

参考上游提交：

- `b21c3e5 feat: auto-apply reasoning_effort=high for hybrid reasoning models`

建议吸收范围：

- `app/control/model/spec.py`
  - 增加模型默认 reasoning effort 字段。
- `app/control/model/registry.py`
  - 对 `grok-4.3`、`grok-4`、`grok-4.20` 等 hybrid reasoning 模型设置默认 `high`。
- `app/products/openai/chat.py`
- `app/products/openai/responses.py`
- `app/products/openai/router.py`
- `app/dataplane/reverse/protocol/xai_console.py`
  - 当调用方没有显式传入 `reasoning_effort` 时才注入默认值。
  - 显式传入 `none/minimal/low/high` 必须覆盖默认值。

不要改变：

- 非 hybrid 模型默认行为。
- 已有 console routing 和 web_search 工具注入逻辑。

新增测试：

```powershell
uv run pytest tests/test_reasoning_effort_defaults.py
uv run pytest tests/test_openai_chat_console_route.py
uv run pytest tests/test_xai_console_protocol.py
```

建议覆盖：

- hybrid 模型未传 `reasoning_effort` 时自动使用 `high`。
- 显式传 `none` 时不被覆盖。
- 显式传 `low/minimal/high` 时原样传给 console。
- 非 hybrid 模型不注入默认 effort。
- 非 console 模型不被误影响。

验收标准：

- 默认行为提升，但调用方控制权不丢失。
- 不改变已有 OpenAI Chat、Responses、Anthropic 转接的路由边界。

## 阶段 5：低风险可选项

### `grok-build-0.1` 模型

价值：低到中。

如果真实可用，单独增加模型注册即可，不应和账号后端改动绑在一起。

测试：

```powershell
uv run pytest tests/test_openai_chat_console_route.py
```

验收：

- 模型出现在 `/v1/models`。
- 路由到 console。
- 不影响现有模型池选择。

### `scripts/inject_cookie.py`

价值：中，适合运维导入 cookie。

建议先作为独立脚本吸收，不耦合账号去重上线。

验收：

- 支持 dry-run。
- 不打印完整 cookie/token。
- 明确写入目标存储。
- 错误时不破坏已有账号数据。

### `tests/test_release_smoke.py`

价值：中。

可以参考其中 health、models、quota 的 smoke 思路，但不能用它替代现有细粒度测试。

## 推荐提交顺序

1. `test: preserve local safety guards before upstream adoption`
2. `feat(admin): add server-side account pagination`
3. `test(admin): cover token pagination filters and metadata`
4. `feat(account): add xai subscription parser`
5. `feat(account): persist account_id and deduplicate accounts`
6. `test(account): cover subscription tier parsing and account_id dedup`
7. `feat(account): add lite account pool tier`
8. `test(account): cover lite/super/heavy pool selection`
9. `feat(console): default hybrid reasoning effort to high`
10. `test(console): cover reasoning effort defaults and overrides`
11. `feat(console): add grok-build model`
12. `chore(scripts): add cookie injection helper`

如果时间有限，优先级应是：

1. Admin 分页。
2. `account_id` 去重和订阅识别。
3. `lite` pool。
4. reasoning effort 默认值。
5. 新模型和脚本。

## 最小可交付版本

如果只做一个高价值 MVP，建议只实现：

- Admin 服务端分页。
- 账号列表 API metadata。
- 保留所有本地安全和代理测试。

原因：该阶段价值明确、风险低、不会触碰生产账号状态，也不会引入外部 subscription API 的不确定性。

## 高价值完整版本

完整版本应包含：

- Admin 服务端分页。
- `account_id` 持久化。
- subscription parser。
- import/refresh 时补全 account identity。
- dry-run 去重报告。
- 确认无误后再启用软删除重复账号。
- `lite` pool。
- reasoning effort 默认值。

完整版本必须把账号去重拆成两个运行模式：

- `observe`：只记录重复组，不改变账号状态。
- `enforce`：软删除重复账号。

默认生产首次上线使用 `observe`，至少观察一轮 refresh 后再改为 `enforce`。

## 验证矩阵

每阶段至少运行：

```powershell
uv run ruff check --fix
uv run pytest tests/test_asset_upload_security.py
uv run pytest tests/test_proxy_host_routing.py
uv run pytest tests/test_console_quota.py
uv run pytest tests/test_openai_chat_console_route.py
uv run pytest tests/test_xai_console_protocol.py
```

按阶段追加：

```powershell
uv run pytest tests/test_admin_tokens_pagination.py
uv run pytest tests/test_xai_subscription.py
uv run pytest tests/test_account_id_dedup.py
uv run pytest tests/test_account_pool_tiers.py
uv run pytest tests/test_reasoning_effort_defaults.py
```

页面改动需要浏览器验证：

- 打开 Admin 账号页。
- 切换分页。
- 修改 page size。
- 使用 pool/status/tags 筛选。
- 确认 console quota pill 仍显示。
- 确认禁用账号状态仍正确。
- 导出账号时确认分页不会漏数据。

## 生产上线前检查

上线前必须确认：

```powershell
git diff --check
uv run ruff check --fix
uv run pytest
```

账号去重阶段上线前必须额外执行：

- 备份账号存储。
- 运行 subscription refresh dry-run。
- 输出重复账号组报告。
- 人工抽查至少 5 组重复账号。
- 确认 soft-delete 只作用于相同 `account_id`。

上线后读回：

- Admin 账号总数、active 数、disabled 数。
- 每个 pool 的账号数。
- `account_id IS NULL` 的 active 账号数。
- soft-deleted 重复账号数。
- 最近 refresh 错误类型分布。
- console 模型调用成功率。
- 资源 URL 上传安全测试仍通过。

## 回滚策略

Admin 分页：

- 回滚该阶段提交即可。
- 不涉及数据迁移。

subscription 和 `account_id`：

- 如果只新增字段，不删除字段，可保留字段并关闭 refresh 逻辑。
- 如果已执行 soft-delete，必须根据审计记录恢复被删除 token。
- 生产首次上线必须先 observe，避免需要批量恢复。

`lite` pool：

- 可临时把 `lite` 账号降级为 `basic` 或升级为 `super`，但必须明确业务影响。
- 不建议自动迁移，除非有真实 subscription 证据。

reasoning effort：

- 删除默认值注入即可。
- 显式传入 effort 的请求不受影响。

## 最终建议

不要把 `piexian/main` 当成一个待合并分支；把它当成一个功能素材库。

实际执行时先做 Admin 分页，拿到低风险高收益。随后做 subscription/account_id，但必须增加 dry-run 和去重审计。`lite` pool 和 reasoning effort 属于增强项，应在账号身份链路稳定后再吸收。
