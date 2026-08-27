# IM12–IM14 受治理的服务端生成桥接验收证据

- 基线：`main@b9852257076ccad2ac8aed8b1e04cefab5e0d901`
- 范围：项目级生成 API、加法式任务/尝试/事件数据、受令牌及租约保护的 Worker API、确定性 fake Worker 流程、受信任 multipart 字节流入库、Material/AssetVersion、权利默认阻断、前端服务端状态接入
- Provider：运行时默认 `disabled`；只有测试构造可使用 `deterministic-fake`
- 禁止项：未新增依赖或锁文件，未修改 MoneyPrinter Adapter 或固定上游，未接入真实模型/插件/API Key/付费调用，未新增队列或对象存储，未自动采纳/时间线写入/渲染/发布，未部署或公开访问

## 40 条强制验收

| # | 验收结果 | 可执行证据 |
|---:|---|---|
| 1 | PASS | `test_01_owner_create_is_202_and_never_calls_provider` |
| 2 | PASS | `test_04_viewer_is_read_only_with_zero_writes` |
| 3 | PASS | `test_05_disabled_provider_blocks_without_writes`、`test_13_unknown_capability_and_revision_conflict_are_rejected` |
| 4 | PASS | `test_37_tenant_scope_hides_foreign_task` |
| 5 | PASS | `test_02_same_idempotency_and_body_returns_same_task` |
| 6 | PASS | `test_03_same_idempotency_different_body_conflicts` |
| 7 | PASS | `test_06_to_12_strict_request_and_capability_validation`（7 个参数化输入） |
| 8 | PASS | `test_14_list_detail_are_scoped_paginated_and_prompt_free` |
| 9 | PASS | `test_15_csrf_protects_generation_mutations` |
| 10 | PASS | `test_36_browser_dto_never_contains_secrets_or_raw_provider_response`、Web API 错误脱敏测试 |
| 11 | PASS | `test_01_owner_create_is_202_and_never_calls_provider` |
| 12 | PASS | `test_34_concurrent_claim_has_single_winner` |
| 13 | PASS | `test_17_heartbeat_requires_current_lease_owner` |
| 14 | PASS | Worker `test_restart_with_upstream_id_queries_without_reposting` |
| 15 | PASS | Worker `test_ambiguous_submission_becomes_unknown_without_repost` |
| 16 | PASS | `test_39_non_retryable_provider_4xx_cannot_be_retried`、Worker 4xx 测试 |
| 17 | PASS | `test_22_expired_lease_is_recovered_without_new_attempt` |
| 18 | PASS | `test_19_cancel_is_idempotent_and_late_success_isolated` |
| 19 | PASS | `test_20_retry_appends_attempt_and_preserves_history` |
| 20 | PASS | `test_18_upstream_id_is_immutable_and_illegal_transition_rejected`、重复完成测试 |
| 21 | PASS | `test_28_json_or_unauthenticated_artifact_intake_is_rejected` |
| 22 | PASS | `test_23_to_27_artifact_intake_rejects_untrusted_or_invalid_payloads` |
| 23 | PASS | 同上（空文件、2 GiB 配置边界）及 `test_38_storage_quota_rejects_before_media_store` |
| 24 | PASS | `test_40_stream_interruption_compensates_quota_and_records_failure` |
| 25 | PASS | `test_29_probe_failure_compensates_quota_and_creates_no_asset` |
| 26 | PASS | `test_30_success_creates_one_material_and_immutable_asset_with_provenance` |
| 27 | PASS | `test_31_repeated_artifact_completion_is_idempotent_without_double_quota` |
| 28 | PASS | 探测失败与流中断补偿测试均验证零部分 AssetVersion、配额归还和失败事件 |
| 29 | PASS | `test_32_rights_default_block_then_allowed_is_derived_without_mutating_evidence` |
| 30 | PASS | `test_32_rights_default_block_then_allowed_is_derived_without_mutating_evidence` 连续覆盖 missing/denied/revoked/not-yet/expired/allowed |
| 31 | PASS | `GenerationPanel` 组件重建从服务端恢复任务测试 |
| 32 | PASS | `GenerationPanel` 旧项目迟到响应隔离测试 |
| 33 | PASS | Viewer 服务端任务只读 UI 测试及 API 零写入测试 |
| 34 | PASS | 权利允许只创建 `adopted=false` 编辑引用测试；未调用时间线、渲染或发布 API |
| 35 | PASS | API、Worker、Web、contracts、editor 全量回归 |
| 36 | PASS | 文件白名单与依赖/锁文件/网络/部署范围审计 |
| 37 | PASS | `test_16_internal_claim_requires_token_and_claims_once`、心跳/迁移/入库租约测试 |
| 38 | PASS | Worker `test_worker_generation_modules_never_import_database_models` |
| 39 | PASS | `test_20_retry_appends_attempt_and_preserves_history`、`test_33_generation_events_are_append_only` |
| 40 | PASS | JSON/URL/路径拒绝与仅 multipart 字节流 + token + lease 入库测试 |

`apps/api/test_generation_tasks.py` 单独执行产生恰好 `40 passed`；Worker 与 Web 测试提供跨模块补充证据。

## 回归结果

| 门禁 | 结果 |
|---|---|
| IM12–IM14 API 强制验收 | `40 passed` |
| API 全量 | `63 passed` |
| Worker 新增 | `8 passed` |
| Worker 全量 | `31 passed` |
| Web 全量 | `56 passed` |
| contracts | `11 passed` |
| editor | `4 passed` |
| TypeScript | PASS |
| ESLint（0 warning） | PASS |
| Web production build | PASS |
| `git diff --check` | PASS |

## 数据与副作用断言

- 生成任务创建只写 `generation_tasks`、首次 `generation_attempts` 与 append-only `generation_events`，不调用 Provider。
- Worker 不导入 ORM/数据库模块；内部接口以 `X-Worker-Token` 与租约所有者共同授权。
- 上游任务 ID 在一个 attempt 内不可替换；模糊提交进入 `UNKNOWN` 且禁止盲目 POST。
- 产物入口不接受浏览器 URL、路径或 JSON 引用，只接受当前受信任 Worker 的 multipart MP4 字节流。
- 成功入库只新增项目 Material 和不可变 AssetVersion；provenance、checksum、Provider 产物编号与 capability snapshot hash 一并保存。
- 新生成 AssetVersion 不自动生成 RightsSnapshot；存储状态固定为 `RIGHTS_BLOCKED`，只有现有权利判定为允许时浏览器 DTO 才派生 `SUCCEEDED`。
- 生成链路不创建 Candidate、Adoption、MasterRevision，不写最终时间线，不触发渲染或发布。
