# IM15–IM17 Provider 激活准备验收证据

- 编码基线：`main@16d987d4265e4fa4aea346f493277b7869585d55`
- 正式评审 HEAD：`54dbbd676426b08325f826a83fde26cbecd66659`
- 合并提交：`main@ad7e505d6d131d12e2c18c5c255a6ae034b62fbd`
- 范围：Provider 非秘密配置与证明、旧旁路退役、受限 Adapter/产物流、配额与用量、持久熔断、紧急停机及服务端权威前端状态
- Provider：源代码、Compose 与环境模板均默认 `disabled`；测试只使用 deterministic fake 行为
- 禁止项：未新增依赖或锁文件，未升级固定上游或 Dockerfile，未接入真实 Provider/模型/插件/凭据/付费调用，未新增队列或外部对象存储，未自动权利允许/采纳/时间线写入/渲染/发布，未部署或公开访问

## 48 条强制验收

| # | 结果 | 可执行证据 |
|---:|---|---|
| 1 | PASS | `test_im15_01_new_environment_is_disabled_without_provider_calls` |
| 2 | PASS | `test_im15_02_unknown_or_disguised_operator_mode_is_disabled` |
| 3 | PASS | `test_im15_03_editor_and_viewer_cannot_mutate_provider_control` |
| 4 | PASS | `test_im15_04_owner_draft_requires_csrf_and_has_no_provider_call` |
| 5 | PASS | `test_im15_05_unknown_url_and_secret_shaped_policy_is_rejected` |
| 6 | PASS | `test_im15_06_published_config_is_immutable_and_new_version_appends` |
| 7 | PASS | `test_im15_07_owner_publish_cannot_override_operator_disabled` |
| 8 | PASS | `test_im15_08_operator_mode_without_published_config_is_disabled` |
| 9 | PASS | `test_im15_09_published_config_without_worker_proof_is_disabled` |
| 10 | PASS | `test_im15_10_mismatched_worker_proof_disables_readiness` |
| 11 | PASS | `test_im15_11_bad_worker_token_has_zero_state_change` |
| 12 | PASS | `test_im15_12_matching_fresh_proof_produces_stable_capability_hash` |
| 13 | PASS | `test_im15_13_superseded_snapshot_hash_is_rejected_without_task` |
| 14 | PASS | `test_im15_14_readiness_is_sanitized_and_legacy_probes_are_gone` |
| 15 | PASS | `test_im15_15_provider_control_recovers_from_persistent_state` |
| 16 | PASS | `test_im15_16_compose_and_environment_templates_default_disabled` |
| 17 | PASS | `test_im16_17_api_moneyprinter_legacy_routes_are_retired` |
| 18 | PASS | `test_im16_18_api_runtime_has_no_provider_adapter_call_path` |
| 19 | PASS | Worker `test_im16_19_moneyprinter_mode_requires_matching_claim_proof` |
| 20 | PASS | Worker `test_im16_20_submit_payload_is_allowlisted` |
| 21 | PASS | Worker `test_im16_21_4xx_is_not_retried_but_429_and_5xx_are_bounded` |
| 22 | PASS | Worker `test_im16_22_ambiguous_post_is_never_replayed` |
| 23 | PASS | Worker `test_im16_23_restart_with_upstream_id_queries_without_reposting` |
| 24 | PASS | Worker `test_im16_24_unknown_upstream_state_stays_unknown` |
| 25 | PASS | Worker `test_im16_25_raw_body_prompt_and_sensitive_headers_are_not_logged` |
| 26 | PASS | Worker `test_im16_26_same_origin_relative_or_absolute_artifact_is_accepted` |
| 27 | PASS | Worker `test_im16_27_external_origin_credentials_protocol_and_traversal_are_rejected` |
| 28 | PASS | Worker `test_im16_28_redirect_is_rejected_without_following` |
| 29 | PASS | Worker `test_im16_29_http_client_disables_proxy_environment` |
| 30 | PASS | Worker `test_im16_30_invalid_or_oversize_stream_never_returns_bytes` |
| 31 | PASS | Worker `test_im16_31_deterministic_fake_stream_is_ingested_once_with_artifact_id` |
| 32 | PASS | Worker `test_im16_32_cancel_is_local_when_upstream_capability_is_absent` |
| 33 | PASS | `test_im17_33_validate_reports_quota_without_reservation` |
| 34 | PASS | `test_im17_34_create_atomically_writes_one_reserved_entry` |
| 35 | PASS | `test_im17_35_concurrent_create_never_exceeds_limit` |
| 36 | PASS | `test_im17_36_monthly_request_and_seconds_limits_reject_new_work` |
| 37 | PASS | `test_im17_37_cancel_releases_once_without_negative_usage` |
| 38 | PASS | `test_im17_38_success_settles_probe_seconds_once` |
| 39 | PASS | `test_im17_39_retry_reuses_reservation_and_appends_attempt` |
| 40 | PASS | `test_im17_40_multiple_worker_failures_open_circuit_once` |
| 41 | PASS | `test_im17_41_open_blocks_claim_and_half_open_allows_one_probe` |
| 42 | PASS | `test_im17_42_half_open_success_closes_persistent_circuit` |
| 43 | PASS | `test_im17_43_owner_emergency_stop_blocks_validate_create_and_claim` |
| 44 | PASS | `test_im17_44_stop_and_recovery_preserve_governance_evidence` |
| 45 | PASS | Web `IM17-45 late readiness and task responses cannot pollute a new project` |
| 46 | PASS | Web `IM17-46 viewer is read-only under server readiness and rights gates` |
| 47 | PASS | `test_im17_47_test_runtime_is_fake_only_and_real_mode_defaults_disabled` |
| 48 | PASS | `test_im17_48_static_scope_has_no_dependency_pin_or_public_provider_egress` |

## 正式评审修复

`FR24-01` 发现：用户取消、租约丢失或 owner 紧急停机后，Worker heartbeat 的 409 曾被压成通用异常，随后可能再次尝试写 `FAILED` 并让第二个 409 逃出主循环。

修复后：

- `GenerationQueueError` 保留去敏后的 HTTP 状态与稳定治理错误码；
- `TASK_CANCELED`、`LEASE_LOST`、`PROVIDER_EMERGENCY_STOPPED` 使 Worker 就地停止；
- Worker 不再覆盖服务端权威终态，也不会因第二次冲突终止主循环；
- `test_generation_queue_preserves_governance_rejection_code` 与 `test_worker_stops_without_overwriting_queue_governance` 覆盖该行为。

## 回归结果

| 门禁 | 结果 |
|---|---|
| IM15–IM17 强制验收 | `48/48 PASS` |
| API 全量 | `99/99 PASS` |
| Worker 全量 | `48/48 PASS` |
| Web 全量 | `56/56 PASS` |
| contracts | `11/11 PASS` |
| editor | `4/4 PASS` |
| video-use | `3/3 PASS` |
| TypeScript / ESLint / production build | PASS |
| Python compile / `git diff --check` | PASS |
| CI Pipeline #126 | PASS |

CI Pipeline #126 的三项作业全部通过：基础 lint/build/unit 与依赖审计、Playwright 工作台流程、Docker Compose 集成。Docker 作业同时通过健康栈、同源网络、Worker/video-use、FFmpeg、固定上游、真实渲染、持久队列及生产浏览器上传到下载闭环。

## 数据与副作用断言

- 旧 `/moneyprinter/*` API 直连旁路稳定返回 410；API 运行时不实例化 Worker Adapter。
- 真实模式需要 operator 开关、owner 已发布配置和新鲜匹配 Worker 证明三重门禁；任一缺失即 fail closed。
- Provider 配置只保存非秘密策略；DTO、事件、日志与异常不暴露地址、凭据、原始 body 或完整提示词。
- Adapter 禁止代理继承与重定向，只接受配置允许的同源产物路径，并限制媒体类型和字节数。
- 配额使用追加式 RESERVED/RELEASED/SETTLED 记录；熔断与停机状态持久化且可审计。
- 停机、取消和租约丢失不会删除或覆盖 task、attempt、event、usage、Material、AssetVersion 或 rights 证据。
- 新生成 AssetVersion 仍默认 rights-blocked；无自动采纳、时间线写入、渲染或发布。

## Repository acceptance closure

- Approval-package PR: [#23](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/pull/23), merged as `16d987d4265e4fa4aea346f493277b7869585d55`.
- Functional PR: [#24](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/pull/24).
- Formally reviewed head: `54dbbd676426b08325f826a83fde26cbecd66659`.
- Squash merge commit: `ad7e505d6d131d12e2c18c5c255a6ae034b62fbd`.
- Final-head CI: [Pipeline #126](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/33056228449) — all three jobs passed.
- Formal review: PASS; blocking findings: 0; unresolved review threads: 0.
- Scope audit: 18 changed files, all within the owner-approved allowlist and CI extension.
- Repository status: **IM15–IM17 implementation accepted**.

This acceptance records repository implementation only. Runtime real Provider remains disabled; real connectivity, credentials, paid use, deployment, public access and commercial operation remain separately unauthorized.
