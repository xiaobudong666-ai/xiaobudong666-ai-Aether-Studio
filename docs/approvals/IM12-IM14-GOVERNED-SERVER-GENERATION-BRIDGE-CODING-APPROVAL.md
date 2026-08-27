# IM12–IM14 受治理的服务端生成桥接——编码审批包

> Status: `DRAFT_FOR_OWNER_APPROVAL`
> Prepared: 2026-08-27 (UTC)
> Authoritative baseline: `main@a41bdf194d92158abb49f83c45189c52b1e9ebd1`
> Depends on: accepted IM9–IM11 deterministic frontend/local workflow
> Decision owner: Aether Studio one-person OPC owner
> This document is not implementation, provider activation, paid use, deployment or commercial approval.

## 1. 请求批准的决策

本审批包请求批准一个后续编码批次，把已经验证的 IM9–IM11 前端本地生成流程连接到仓库现有的 FastAPI、SQLite WAL、Worker、MoneyPrinterTurbo Adapter、素材版本与权利治理能力。

本批次只包含三个实现单元：

1. **IM12 项目级生成 API 与能力门禁**：服务端校验身份、角色、租户、项目、能力快照、输入与幂等键，异步创建规范任务。
2. **IM13 可恢复的 Worker 生成任务编排**：复用现有租约模式，实现认领、心跳、提交、轮询、取消、有限重试与模糊响应恢复。
3. **IM14 受信任生成产物入库**：从受控 Sidecar 产物引用流式写入，执行来源、大小、配额、媒体探测与 SHA-256 校验，创建 Material 与不可变 AssetVersion，并强制进入权利阻断状态。

编码授权、正式评审、合并、真实上游激活与部署必须分别批准。此文档进入 `main` 只代表设计边界被接受。

## 2. 基线事实

### 2.1 已实现且必须复用

- IM9–IM11 的 React 生成预检、deterministic fake/local 状态机、取消/重试、attempt 历史、版本化浏览器快照、结果 provenance/rights/checksum 审阅和 `adopted=false` 编辑器引用。
- owner/editor/viewer RBAC、Session、CSRF、租户与项目隔离。
- SQLite WAL、加法式迁移、RenderTask 租约队列、Worker 恢复模式。
- Material 上传、租户存储配额、媒体探测、SHA-256、不可变 AssetVersion、RightsSnapshot 与 rights-check。
- 固定 MoneyPrinterTurbo `v1.2.7` / commit `475f21147f0808f5ffe3f58af9ab794b28a4da2c` Adapter。

### 2.2 已核实缺口

- 当前浏览器生成任务只保存在版本化本地快照，不能跨设备、跨浏览器或由 Worker 权威恢复。
- `/moneyprinter/generate` 当前同步调用上游并写入语义有限的 `external_tasks`；它没有 project、请求快照、幂等、租约、进度、错误和产物引用。
- `/moneyprinter/health` 与 `/moneyprinter/capabilities` 尚未纳入登录、项目与去敏能力快照门禁。
- Worker 的 MoneyPrinter 路径是演示函数，不属于持久任务认领循环。
- 上游产物未经过受信任来源、流式限制、配额、探测、哈希与 AssetVersion 入库。
- 当前生成路径不能证明 `RIGHTS_MISSING` 会在服务端阻止快速制作、渲染、采纳与时间线写入。

## 3. 权威架构选择

- 新建独立 `generation_tasks` 表；不扩充含义模糊的 `external_tasks`，也不把生成任务伪装成 `render_tasks`。
- 浏览器只访问同源 FastAPI；Sidecar 地址、上游 job/artifact URL、令牌和原始错误不得进入前端。
- API 只持久化和排队，返回 `202`；生成提交和轮询只由 Worker 执行。
- 默认运行模式为 `disabled`；自动测试只注入 deterministic fake Adapter。真实 MoneyPrinter 网络调用必须另行通过激活门禁。
- 复用现有数据库、Worker 进程和持久队列模式；不增加队列基础设施或第三方依赖。
- 产物入库后不创建 Candidate、Adoption、MasterRevision、时间线或 RenderTask；只有权利显式允许后，现有 QuickCreate 才能引用 AssetVersion。

## 4. IM12——项目级生成 API 与能力门禁

### 4.1 API 合同

| 方法与路径 | 权限 | 行为 |
|---|---|---|
| `GET /generation/providers/moneyprinter/capabilities` | owner/editor/viewer | 返回去敏、短时有效能力快照；未启用时如实禁用 |
| `POST /projects/{project_id}/generation-tasks/validate` | owner/editor | 校验输入、项目、能力与配额，不创建任务、不调用上游 |
| `POST /projects/{project_id}/generation-tasks` | owner/editor | 以租户级幂等键创建任务并排队，返回 `202` |
| `GET /projects/{project_id}/generation-tasks` | owner/editor/viewer | 分页列出当前项目任务 |
| `GET /projects/{project_id}/generation-tasks/{task_id}` | owner/editor/viewer | 读取状态、进度、attempt、去敏错误和产物引用 |
| `POST /projects/{project_id}/generation-tasks/{task_id}/cancel` | owner/editor | 仅对允许状态发出取消意图，幂等返回 |
| `POST /projects/{project_id}/generation-tasks/{task_id}/retry` | owner/editor | 从允许失败状态创建新 attempt，不改写历史 |

所有写请求沿用现有 CSRF；所有读写同时检查 session tenant 与 project。其他租户 UUID 返回 404 或等价非泄露响应。

Worker 只能通过既有内部令牌访问以下服务端合同，不得直接连接或写入业务数据库：

| 方法与路径 | 调用方 | 行为 |
|---|---|---|
| `POST /internal/generation-tasks/claim` | Worker | 原子认领一条可执行任务并返回租约、请求快照和取消意图 |
| `POST /internal/generation-tasks/{task_id}/heartbeat` | 持有租约的 Worker | 延长租约并更新规范进度；租约不匹配返回冲突 |
| `POST /internal/generation-tasks/{task_id}/transition` | 持有租约的 Worker | 追加状态事件并幂等保存 upstreamJobId、attempt 结果或去敏错误 |
| `POST /internal/generation-tasks/{task_id}/artifact-intake` | 持有租约的 Worker | 接收流式 multipart 产物与 providerArtifactId；API 执行校验、入库事务和权利阻断 |

内部端点必须使用现有恒时 Worker token 校验；缺失、错误或租约不匹配时零状态变化、零素材写入。`artifact-intake` 不接受 URL、宿主机路径或 JSON 中的文件位置，只接受请求体字节流和受限元数据。

### 4.2 输入与能力

请求字段限于：`videoSubject`（1–500）、`videoAspect`（`9:16/16:9/1:1`）、`voiceName`（能力枚举）、`videoConcatMode`（`random/sequential`）、`videoClipDuration`（1–10）、`outputCount`（1–4）、`confirmExternalGeneration` 与 UUID `idempotencyKey`。

能力快照至少包含 `provider/enabled/healthy/checkedAt/expiresAt/sourceVersion`、受限枚举、数值范围与去敏 `reasonCode`。提交时服务端必须重新验证快照未过期且与请求一致；前端显示不能替代服务端门禁。

本批不接收真人身份、脸部模板、声音克隆样本、支付信息、密钥、任意 URL 或任意模型参数。

## 5. IM13——Worker 任务编排

### 5.1 规范状态

`QUEUED → SUBMITTING → RUNNING → INGESTING → RIGHTS_BLOCKED`；明确失败可进入 `FAILED`，取消进入 `CANCELED`，不确定响应进入 `UNKNOWN`，部分入库进入 `PARTIAL`。`SUCCEEDED` 只作为实时权利允许后的派生可用状态，不覆盖原始入库证据。

终态不可被迟到响应覆盖；非法倒退必须拒绝并记录审计事件。

### 5.2 幂等、租约与恢复

1. API 在任何上游调用前以 `tenant_id + idempotency_key` 原子创建任务。
2. 同键同请求返回既有任务；同键不同请求返回 `409`。
3. 多个 Worker 并发认领时只能有一个获得租约。
4. Worker 提交前检查 `upstream_job_id`；已存在时只能继续查询。
5. 明确 4xx 不重试；明确 5xx/前置连接失败按 `max_attempts` 有界退避。
6. 请求可能已到达上游但响应丢失时进入 `UNKNOWN`，按幂等键或已知 jobId 恢复，禁止盲目再次 POST。
7. Worker 重启后回收过期租约；重复完成回调和轮询结果必须幂等。
8. 取消是持久意图；上游不支持取消时仍不得把取消后的迟到结果自动用于下游。
9. attempt 历史追加保存，不覆盖旧 attempt。
10. 每次提交或 retry 创建不可变 attempt；每次状态变化、取消、重试、租约回收和入库结果追加不可变 event。
11. 日志、DTO 与错误只包含规范错误码和去敏信息。

## 6. IM14——受信任产物入库与权利交接

Worker 只接受 Adapter 从已知内部 Sidecar 返回的产物标识或同源相对下载路径。拒绝浏览器提供的 URL、外部重定向、`file://`、宿主机路径、shell 参数和未配置对象存储地址。Worker 完成来源校验与 Sidecar 流读取后，通过内部 `artifact-intake` 把字节流交给 API；API 是配额、业务记录与事务的唯一写入权威。

入库必须按以下顺序：

1. 校验 task、tenant、project、requestedBy、租约与 providerArtifactId。
2. 解析并再次验证 scheme/host/path；默认禁止重定向。
3. 流式写入受控临时文件，同时检查 Content-Length、2 GiB 硬上限和租户剩余配额。
4. 计算 SHA-256，调用现有媒体探测取得 container/codec/duration/width/height/fps。
5. 空文件、超限、配额不足、来源异常、探测失败或哈希失败不得创建业务记录，并清理临时文件。
6. 在一个事务中创建一个 Material 与一个不可变 AssetVersion，准确增加 storage usage。
7. 保存 generationTaskId、provider、upstreamJobId、providerArtifactId、inputSnapshotHash、capabilitySnapshotHash、sha256 与 ingestedAt。
8. 以 `generation_task_id + provider_artifact_id` 保证重复完成不会重复素材或重复计量。
9. 不自动创建 RightsSnapshot，任务进入 `RIGHTS_BLOCKED`；只有现有 rights-check 返回 `RIGHTS_ALLOWED` 才能用于 QuickCreate。

## 7. 数据结构

`DBGenerationTask` 至少包含：`id/tenant_id/project_id/requested_by/provider/status/progress/message/request_json/request_hash/capability_snapshot_json/capability_snapshot_hash/idempotency_key/upstream_job_id/provider_artifact_id/media_id/asset_version_id/attempts/max_attempts/lease_owner/lease_expires_at/cancel_requested_at/error_code/error_message/created_at/updated_at/started_at/completed_at`。

`DBGenerationAttempt` 为不可变 attempt 权威记录，至少包含：`id/generation_task_id/attempt_no/status/submission_started_at/upstream_job_id/reconciliation_state/error_code/error_message/created_at/completed_at`，并设置 `UNIQUE(generation_task_id,attempt_no)`。retry 只能追加下一编号，不得覆盖旧记录。

`DBGenerationEvent` 为追加式审计记录，至少包含：`id/generation_task_id/attempt_id/event_type/from_status/to_status/actor_type/actor_id/metadata_json/created_at`。`metadata_json` 只允许受限去敏字段，不保存提示词、URL、令牌或上游原始响应。

约束：`UNIQUE(tenant_id,idempotency_key)`；`UNIQUE(generation_task_id,attempt_no)`；项目、租户、素材和版本必须一致；迁移只能加表/索引，不删除、重写或回填不相关业务数据。

## 8. 前端切换边界

- 保留 deterministic local adapter 作为测试替身，不在生产 UI 伪装为真实成功。
- `GenerationPanel` 改为消费受保护 API 状态；项目切换、组件卸载或迟到响应不能污染新项目。
- 本地草稿可保留；权威任务、attempt、结果与审计来自服务端。
- 只有 `assetVersionId + RIGHTS_ALLOWED` 才显示“用于快速制作”。
- 创建的编辑器引用继续为 `adopted=false`，不得自动写最终时间线。

## 9. 权限和安全

| 操作 | owner | editor | viewer | Worker |
|---|---:|---:|---:|---:|
| 查看能力和本项目任务 | 是 | 是 | 是 | 必要字段 |
| 校验、提交、取消、重试 | 是 | 是 | 否 | 否 |
| 认领、心跳、更新、入库 | 否 | 否 | 否 | 内部令牌 |
| 记录 RightsSnapshot | 现有规则 | 现有规则 | 否 | 否 |
| 自动采纳、发布或绕过权利 | 否 | 否 | 否 | 否 |

敏感输入不得出现在 URL、普通日志、分析事件、SSE、客户端存储或原始异常。内部 Worker 路由沿用现有恒时令牌比较和去敏错误边界。

## 10. 40 条强制验收

### API 与隔离

1. owner/editor 在健康 fake capability 下提交有效请求，返回 `202` 与唯一 taskId。
2. viewer 提交返回 `403`，零任务、零上游调用。
3. disabled/unhealthy/unknown/expired capability 下提交被拒绝且零上游调用。
4. 非当前租户项目不可读写且不泄露存在性。
5. 同幂等键同请求返回同任务，双击只产生一条记录。
6. 同幂等键不同请求返回 `409`，原任务不变。
7. 输入枚举、长度、整数和确认字段在服务端严格校验。
8. 列表与详情只返回当前项目，并稳定分页。
9. CSRF 缺失或无效的写请求被拒绝。
10. DTO、错误和日志不包含 Sidecar 地址、密钥、Authorization、完整提示词或原始响应。

### Worker 与恢复

11. API 创建任务不直接访问 Sidecar。
12. 两个 Worker 并发 claim 只有一个成功。
13. 心跳只能由持有租约的 Worker 更新。
14. 已保存 upstreamJobId 的重启任务只查询，不重复提交。
15. 模糊网络响应进入 `UNKNOWN`，恢复后提交 POST 次数仍为 1。
16. 明确 4xx 不重试；明确可重试失败受 maxAttempts 限制。
17. 过期租约可回收，未过期租约不可抢占。
18. 取消请求幂等；取消后的迟到成功不能覆盖状态或自动入库。
19. retry 追加新 attempt，旧 attempt 与审计历史不变。
20. 非法状态倒退、终态覆盖和重复 complete 被安全拒绝或幂等吸收。

### 产物与权利

21. 浏览器提交任意 artifact URL 被拒绝。
22. 外部 host、协议异常、路径逃逸和重定向被拒绝。
23. 超过 2 GiB、超过租户配额或空产物不创建 Material/AssetVersion。
24. 流中断清理临时文件且不增加 storage usage。
25. 探测失败或媒体类型不允许时不创建业务记录。
26. 成功入库产生一个 Material、一个不可变 AssetVersion 和正确 SHA-256/provenance。
27. 重复完成返回同一 AssetVersion，不重复计量。
28. 数据库事务失败不留下半成品记录，存储补偿可审计。
29. 新产物没有 RightsSnapshot，状态为 `RIGHTS_BLOCKED/RIGHTS_MISSING`。
30. 权利缺失、拒绝、撤回、未生效或过期时 QuickCreate 和渲染继续阻断。

### UI、回归与范围

31. 页面刷新/跨组件重建后从服务端恢复任务、attempt 与结果。
32. 项目切换期间的迟到响应不会写入新项目 UI。
33. viewer 只读；owner/editor 才能提交、取消和重试。
34. 权利允许后只创建 `adopted=false` 引用，不自动写最终时间线或提交渲染。
35. 现有 51 个 Web 测试、API、Worker、Playwright、Docker、FFmpeg、video-use、真实渲染和上传到下载回归全部通过。
36. diff 范围审计证明无依赖、锁文件、Adapter pin、真实凭据、部署和公开访问变化。
37. 四个内部 Worker 端点在令牌缺失、错误或租约不匹配时零状态变化、零素材写入。
38. Worker 代码不导入数据库 Session/模型，也不直接写业务数据库；所有权威更新经内部 API 完成。
39. 初次提交和每次 retry 分别产生唯一 attempt；重启、重复回调和迟到响应不覆盖历史 attempt/event。
40. `artifact-intake` 只接受持有租约 Worker 的 multipart 字节流与受限元数据，拒绝 URL、路径和非流式 JSON 产物引用。

## 11. 允许的编码文件范围

后续只有在 owner 明确批准编码后，才允许修改或新增：

- `apps/api/app/models.py`
- `apps/api/app/migrations.py`
- `apps/api/app/schemas.py`
- `apps/api/app/main.py`
- `apps/api/app/generation_tasks.py`
- `apps/api/test_generation_tasks.py`
- `apps/worker/app/main.py`
- `apps/worker/app/generation_queue.py`
- `apps/worker/test_generation_tasks.py`
- `apps/web/src/generation.ts`
- `apps/web/src/generation.test.ts`
- `apps/web/src/components/GenerationPanel.tsx`
- `apps/web/src/components/GenerationPanel.test.tsx`
- 与该功能 PR 验证事实对应的 IM12–IM14 evidence 文档

若实现证明必须修改此清单之外的功能文件、依赖清单、Docker/部署配置、Adapter 或上游 pin，必须停止并重新申请扩大范围。

## 12. 明确禁止范围

- 真实 provider/plugin/model/API key、生产数据或付费调用。
- MoneyPrinterTurbo Adapter 行为或固定上游版本升级。
- 新依赖、lockfile、第二个提供商、队列基础设施或外部对象存储。
- 数字人、声音克隆、换脸/换人、换装、背景替换或身份模板。
- 自动权利允许、自动采纳、自动母版、自动时间线写入、自动渲染或发布。
- 部署、公开访问、生产配置、商业运营。

## 13. 门禁顺序

1. 本文档 Draft PR 的 CI、范围审计与自检。
2. owner 批准文档转正式评审。
3. owner 批准文档合并。
4. owner 对精确 `main` SHA 和第 11/12 节明确批准编码。
5. 实现、40 条验收、全量回归与 Draft 功能 PR。
6. owner 分别批准正式评审和合并。
7. 真实上游激活、密钥、付费调用与部署仍需新的独立审批。
