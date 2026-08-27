# IM15–IM17 受治理的 Provider 激活准备——编码审批包

> Status: `DRAFT_FOR_OWNER_APPROVAL`
> Prepared: 2026-08-27 (UTC)
> Authoritative baseline: `main@db2a23bc95e7990f2652b5fe38c625ce232a16de`
> Depends on: accepted IM12–IM14 governed server-generation bridge
> Decision owner: Aether Studio one-person OPC owner
> This document is not real-provider activation, credential approval, paid-use approval, deployment approval or commercial approval.

## 1. 请求批准的决策

本审批包请求批准一个后续编码批次，为已经合并的 IM12–IM14 服务端生成桥接增加“真实上游激活前必须存在”的控制面。实现完成后，仓库仍必须默认关闭真实 Provider，自动测试仍只能使用 deterministic fake Sidecar；任何真实上游、API Key、付费调用或部署必须另行批准。

本批次只包含三个实现单元：

1. **IM15 — 版本化 Provider 配置、双钥匙启用与能力证明**：API 保存不含秘密的配置版本，Worker 报告去敏、短时有效的运行态能力；只有“已发布配置 + operator 环境开关 + 新鲜且匹配的 Worker 证明”三者同时满足时才允许创建真实模式任务。
2. **IM16 — MoneyPrinter Adapter 合同加固与受限产物流**：补齐提交、查询、取消能力声明、状态规范化和只读流式产物下载；固定内部来源、禁止重定向、代理环境、任意 URL、路径逃逸和原始上游响应外泄。
3. **IM17 — 生成配额、预留/结算、熔断与紧急停机**：增加租户级并发与月度生成用量控制、追加式用量记录、失败熔断和 owner 紧急停机；所有拒绝必须发生在上游调用前并可审计。

文档合并、编码授权、功能正式评审、功能合并、真实上游激活和部署必须分别批准。

## 2. 基线事实与已核实缺口

### 2.1 已实现且必须复用

- 项目级生成 validate/create/list/detail/cancel/retry API，Session、tenant、project、RBAC 与 CSRF 边界。
- `DBGenerationTask`、不可变 attempt、追加式 event、幂等键、Worker claim/heartbeat/transition 和过期租约恢复。
- Worker token 与租约双重保护的 multipart 产物入库。
- 配额、流式写入、媒体探测、SHA-256、Material、不可变 AssetVersion、provenance 和权利默认阻断。
- 服务端权威的 GenerationPanel 状态恢复、项目切换隔离及 `adopted=false` 编辑器引用。
- 固定 MoneyPrinterTurbo `v1.2.7` / commit `475f21147f0808f5ffe3f58af9ab794b28a4da2c`，现有依赖与内部 Sidecar 网络。

### 2.2 当前仓库证据

- `build_capability_snapshot()` 仅在 `mode == "deterministic-fake"` 时启用；其他模式固定返回 `PROVIDER_DISABLED`。
- Worker 的 `process_generation_task()` 只接受 claim 中的 `deterministic-fake`，否则立即失败。
- API 与 Worker 没有可比较的已发布配置版本或运行态配置摘要；能力页面无法证明两端配置一致。
- MoneyPrinter Adapter 当前能提交与查询，但没有已实现的受限 `stream_artifact()` 合同；取消、重定向、来源与响应去敏边界也未形成激活证据。
- Compose 和环境模板没有真实生成运行模式、默认关闭断言、生成并发/月度用量或熔断配置。
- 已有配额覆盖项目、存储、并发渲染和月度渲染秒数；尚无生成请求的预留、结算、并发或月度用量权威记录。

### 2.3 为什么不能直接激活

直接把现有 `disabled` 改为真实 Provider 会让浏览器看到的能力、API 接受任务的条件、Worker 实际配置和 Sidecar 运行态之间缺少一致性证明；失败风暴也没有生成专用的熔断与紧急停机边界。该做法会把密钥、费用与产物来源风险留到真实调用之后处理，不满足受治理激活要求。

## 3. 权威架构选择

- **默认拒绝**：代码、Compose 与两个环境模板的默认真实运行模式必须为 `disabled`。
- **双钥匙加新鲜证明**：浏览器或数据库配置不能单独启用 Provider；operator 环境模式、owner 已发布配置和 Worker 新鲜证明必须一致。
- **秘密不入库**：配置版本只保存非秘密策略、能力范围和摘要；API Key、令牌、Cookie、Authorization 与 Sidecar 原始配置不得进入数据库、API DTO、日志、事件或前端存储。
- **API 为任务与用量权威**：Worker 不直接连接业务数据库；预留、结算、熔断和停机均通过受令牌保护的内部 API 完成。
- **Adapter 为唯一上游边界**：Worker 不能自行拼接 URL 或用通用 HTTP 客户端绕过 Adapter；API 不直接调用真实生成上游。
- **测试网络封闭**：所有新增验收使用 deterministic fake Sidecar；CI 不读取真实密钥、不访问公网 Provider、不产生费用。
- **上游固定不变**：本批不升级 MoneyPrinterTurbo、Docker 基础镜像、依赖或锁文件。

## 4. IM15——配置版本、双钥匙启用与能力证明

### 4.1 配置版本

新增追加式 `DBGenerationProviderConfigVersion`，至少包含：

`id/provider/version/status/policy_json/policy_hash/created_by/created_at/published_by/published_at/supersedes_id`。

`policy_json` 仅允许以下非秘密字段：

- `enabledIntent`：owner 是否批准该配置进入可激活候选；
- `allowedAspects/allowedVoices/allowedConcatModes`；
- `maxClipDurationSeconds/maxOutputs`；
- `concurrentTaskLimit/monthlyRequestLimit/monthlyGeneratedSecondsLimit`；
- `failureWindow/failureThreshold/cooldownSeconds`；
- `artifactPathPrefixes/maxArtifactBytes`；
- `configLabel` 与说明。

配置版本不可修改；发布只能把一个 `DRAFT` 版本原子变为 `PUBLISHED` 并使旧版本成为 `SUPERSEDED`。任何未知字段、秘密形态字段或 URL 字段必须拒绝。

### 4.2 Worker 运行态证明

Worker 周期性通过内部令牌端点报告去敏证明，至少包含：

`provider/operatorMode/configVersionId/policyHash/adapterVersion/upstreamPin/healthy/capabilities/reasonCode/checkedAt/expiresAt/workerId`。

服务端只缓存短时证明；过期、worker/token 不匹配、policy hash 不一致、上游 pin 不一致、operator mode 不是显式 `moneyprinter` 或 owner 配置未发布时，公开 capability 必须返回 `enabled=false`。证明不得包含 Sidecar URL、IP、端口、密钥、完整异常或原始响应。

### 4.3 API 与权限

| 方法与路径 | 权限 | 行为 |
|---|---|---|
| `GET /generation/providers/moneyprinter/readiness` | owner/editor/viewer | 返回去敏的禁用原因、能力、配置版本与证明新鲜度 |
| `GET /generation/providers/moneyprinter/config-versions` | owner | 列出非秘密配置版本 |
| `POST /generation/providers/moneyprinter/config-versions` | owner + CSRF | 新建不可变草稿，不触发上游 |
| `POST /generation/providers/moneyprinter/config-versions/{id}/publish` | owner + CSRF | 发布非秘密策略；operator mode 仍可保持关闭 |
| `POST /generation/providers/moneyprinter/kill-switch` | owner + CSRF | 立即阻止新 claim/提交并记录原因 |
| `POST /internal/generation/providers/moneyprinter/attest` | Worker token | 保存去敏、短时运行态证明 |

配置发布不等于真实激活。即使 owner 发布 `enabledIntent=true`，只要 operator mode 默认值仍为 `disabled`，API 就必须拒绝创建真实模式任务且 Worker 不得访问 Sidecar 生成端点。

## 5. IM16——MoneyPrinter Adapter 合同加固与受限产物流

### 5.1 允许的 Adapter 行为

- 以现有固定 Sidecar base URL 调用已审计的提交与状态端点。
- 将上游状态映射为有限规范集合，不把未知状态伪装为成功。
- 只从配置允许的同源相对路径或严格同源绝对 URL 获取产物。
- 使用 `httpx` 现有依赖、`trust_env=False`、`follow_redirects=False` 和流式响应；先检查响应头，再边读边限制字节数。
- 返回只读二进制流和独立的 `providerArtifactId`；不得把 URL 或宿主机路径传给 API。
- 把 4xx、429、5xx、连接前失败、读取超时、模糊提交、无效 JSON、未知状态和产物错误映射为稳定错误码。
- 仅在上游明确声明取消能力时调用取消；否则保存本地取消意图并隔离迟到结果。

### 5.2 强制拒绝

- `http/https` 之外协议、用户信息、非配置 host/port、外部重定向、路径逃逸、查询中疑似密钥、`file://`、宿主路径与任意浏览器 URL。
- Content-Length 超限、缺失且流超限、空文件、错误 Content-Type、读取中断和 providerArtifactId 不一致。
- 上游原始 body、完整提示词、URL、Authorization、Cookie、API Key 或令牌进入普通日志、DTO、审计事件和测试快照。

### 5.3 提交与恢复

- API 创建任务仍不访问 Sidecar。
- Worker 在保存 attempt 与租约后才提交；得到 upstream job id 后先持久化再轮询。
- 响应丢失且不能证明未提交时进入 `UNKNOWN`，禁止自动再次 POST。
- 已有 upstream job id 的重启任务只查询；终态、取消和过期租约规则沿用 IM13。
- 真实 Adapter 逻辑只能在 operator mode 明确为 `moneyprinter` 且 claim 证明匹配时执行；测试模式注入 fake，不读取 operator 密钥。

## 6. IM17——生成配额、用量、熔断与紧急停机

### 6.1 预留与结算

新增追加式 `DBGenerationUsageEntry`，至少包含：

`id/tenant_id/project_id/task_id/attempt_id/kind/request_units/generated_seconds/reservation_key/config_version_id/created_at`。

`kind` 限于 `RESERVED/RELEASED/SETTLED/ADJUSTED`。相同 `reservation_key + kind` 幂等；记录不可修改或删除。

- validate 只返回当前余量，不预留。
- create 在同一事务内检查并发与月度限额并写 `RESERVED`；超限时零任务、零上游调用。
- 明确未提交、取消或失败释放未使用预留；成功入库按可信媒体探测时长写 `SETTLED`。
- 重试使用同一任务预算，但每次 attempt 必须再次检查并发与熔断；不得重复计量同一产物。
- 用量只记录请求单位与生成秒数，不宣称货币价格、账单或支付结算。

### 6.2 熔断器

新增每租户/Provider 的持久熔断状态：`CLOSED/OPEN/HALF_OPEN/DISABLED`。在配置窗口内达到失败阈值时原子打开；冷却前拒绝新提交但允许读取既有任务。半开只允许一个探测 attempt，成功关闭，失败重新打开。API 重启和多个 Worker 并发不得清空或绕过熔断状态。

### 6.3 紧急停机

owner 可设置去敏原因码并立即停机。停机后：

- 新 validate/create 返回稳定禁用码；
- 未提交任务不再 claim；
- 已提交任务只允许安全查询、持久取消、隔离迟到结果和必要的资源清理；
- 已入库 AssetVersion、attempt、event、rights 与用量证据不得删除或改写；
- 恢复必须是另一条 owner 审计事件，并且仍需满足双钥匙与新鲜证明。

## 7. 前端边界

- `GenerationPanel` 显示服务端权威 readiness、禁用原因、配额余量与熔断/停机状态。
- viewer 只读；owner/editor 的生成按钮只根据服务端 capability DTO 控制，客户端判断不能替代 API 校验。
- 不提供秘密输入框，不展示 Sidecar 地址、上游 job/artifact URL 或原始错误。
- 配置发布与紧急停机保持 owner-only；本批不新增生产运营后台或付费计划页面。
- 权利允许前仍不能用于 QuickCreate；权利允许后仍只创建 `adopted=false` 引用。

## 8. 安全与审计不变量

- 真实运行模式默认关闭；空值、拼写错误和未知模式都按关闭处理。
- API Key 和 Provider 凭据只属于 Sidecar/operator 外部配置，本批不存储、不迁移、不展示。
- Worker 证明、配置发布、熔断变化、停机/恢复、预留与结算均追加审计事件。
- 审计 metadata 使用允许列表；拒绝自由文本秘密和上游原始响应。
- 失败路径先拒绝、后返回；不得为了“检查凭据”产生生成或付费调用。
- CI 必须证明没有公网 Provider egress，并证明默认 Compose 配置无法执行真实生成。

## 9. 48 条强制验收

### IM15：配置与能力证明（1–16）

1. 新环境无配置版本时 readiness 为 disabled，零上游调用。
2. operator mode 缺失、空值、未知值或大小写伪装均按 disabled。
3. editor/viewer 不能创建、发布配置或切换停机状态。
4. owner 新建配置草稿需要有效 CSRF，且不触发上游。
5. 配置未知字段、URL 字段、疑似 secret/token/key 字段被拒绝。
6. 已发布配置不可修改；新版本必须追加并引用 supersedes。
7. 仅发布配置但 operator mode disabled 时仍不能提交真实任务。
8. 仅 operator mode 为 moneyprinter、但无已发布配置时仍不能提交。
9. 配置与 operator mode 就绪、但无新鲜 Worker 证明时仍不能提交。
10. 证明过期、policy hash 不同、pin 不同或 provider 不同均禁用。
11. 错误 Worker token 的证明请求零状态变化。
12. 新鲜匹配证明生成有限 capability snapshot 与稳定 hash。
13. snapshot 到期或配置 supersede 后旧 hash 提交返回冲突且零上游调用。
14. readiness DTO 不包含 Sidecar URL、主机、端口、密钥或原始异常。
15. API/Worker 重启后从持久配置恢复，默认状态不被错误提升为 enabled。
16. Compose、根环境模板和生产环境模板都明确默认 `disabled`。

### IM16：Adapter 与产物流（17–32）

17. API create 只排队，不直接调用 MoneyPrinter Sidecar。
18. disabled/fake 测试模式不会实例化或调用真实 Adapter 网络路径。
19. 明确 moneyprinter 模式只在 claim 配置摘要匹配时进入 Adapter。
20. 提交 payload 只包含审批字段，拒绝任意模型参数、URL 与身份样本。
21. 上游 4xx 不自动重试；429/5xx/连接前失败受边界重试限制。
22. 模糊提交进入 UNKNOWN，自动 POST 次数保持 1。
23. 已保存 upstream job id 的重启任务只查询，不重复提交。
24. 未知上游状态不得映射为 completed/succeeded。
25. 上游原始 body、完整提示词和敏感头不进入日志或 DTO。
26. 产物只接受配置允许的同源相对路径或严格同源绝对 URL。
27. 外部 host/port、用户信息、协议异常、路径逃逸与 `file://` 被拒绝。
28. 301/302/307/308 重定向被拒绝，不跟随到外部源。
29. `trust_env=False`，HTTP(S)_PROXY 不能改变 Sidecar 或产物来源。
30. 空流、超限流、Content-Length 超限、错误类型和中断流不调用 artifact-intake。
31. 有效 fake 流计算/入库结果与 providerArtifactId 关联，重复完成不重复素材或计量。
32. 上游不支持取消时只保存取消意图；迟到成功不入库、不覆盖终态。

### IM17：配额、熔断、停机与回归（33–48）

33. validate 返回并发/月度余量，但不写 reservation。
34. create 在任务同事务写唯一 RESERVED，用量不足时零任务、零上游调用。
35. 并发 create 不能超过租户并发生成限额。
36. 月度请求或生成秒数达到限额时稳定拒绝且不影响既有任务读取。
37. 明确未提交、取消或失败幂等 RELEASED，不产生负用量。
38. 成功入库按可信 probe 时长 SETTLED，重复回调不重复结算。
39. 重试追加 attempt 并复用任务预算，不覆盖历史 usage/event。
40. 多 Worker 同时报告失败只触发一次原子熔断状态变化。
41. OPEN 冷却期拒绝新提交；HALF_OPEN 只允许一个探测 attempt。
42. 半开成功关闭、失败重开；API 重启不清空熔断状态。
43. owner 紧急停机立即阻止新 validate/create/claim，且记录追加事件。
44. 停机不删除已入库素材、rights、attempt、event 或 usage；恢复也需审计。
45. 项目切换与迟到 readiness/task 响应不会污染新项目 UI。
46. viewer 只读；owner/editor 受服务端 capability、配额与权利三重门禁。
47. 全量 API、Worker、Web、Playwright、Docker、FFmpeg、video-use、真实渲染与生产浏览器回归通过；新增测试只使用 deterministic fake Sidecar。
48. diff/CI 审计证明无依赖或锁文件、无上游 pin 升级、无真实密钥/模型/插件、无公网 Provider egress、无付费调用、无部署或公开访问。

## 10. 允许的编码文件范围

只有在 owner 对精确 `main` SHA 明确批准编码后，才允许修改或新增：

- `apps/api/app/models.py`
- `apps/api/app/migrations.py`
- `apps/api/app/schemas.py`
- `apps/api/app/main.py`
- `apps/api/app/generation_tasks.py`
- `apps/api/test_generation_tasks.py`
- `apps/worker/app/main.py`
- `apps/worker/app/generation_queue.py`
- `apps/worker/app/moneyprinter_adapter.py`
- `apps/worker/test_generation_tasks.py`
- `apps/worker/test_moneyprinter.py`
- `apps/web/src/generation.ts`
- `apps/web/src/generation.test.ts`
- `apps/web/src/components/GenerationPanel.tsx`
- `apps/web/src/components/GenerationPanel.test.tsx`
- `.env.example`
- `infra/docker/.env.example`
- `infra/docker/docker-compose.yml`
- 与该功能 PR 验证事实对应的 IM15–IM17 evidence 文档

若实现证明必须修改此清单之外的功能文件、依赖/锁文件、MoneyPrinter 固定上游、Dockerfile、Nginx、生产脚本或其他服务，必须停止并重新申请扩大范围。

## 11. 明确禁止范围

- 在任何已提交配置中把真实 Provider 默认设为 enabled/active/moneyprinter。
- 真实 Provider/plugin/model、API Key、Cookie、令牌、生产数据或付费调用。
- 在 CI、开发验证或评审中访问公网 Provider；所有网络验收只允许 deterministic fake Sidecar。
- MoneyPrinterTurbo 上游版本/commit、Dockerfile、依赖或锁文件升级。
- 新队列基础设施、外部对象存储、秘密管理服务、支付/账单系统或第二个 Provider。
- 数字人、声音克隆、换脸/换人、换装、背景替换或身份模板。
- 自动权利允许、自动采纳、自动母版、自动时间线写入、自动渲染或发布。
- 部署、公开访问、生产配置变更、真实激活或商业运营。

## 12. 门禁顺序

1. 本文档 Draft PR 的 CI、JSON、范围审计与自检。
2. owner 批准文档转正式评审。
3. owner 批准文档合并。
4. owner 对新的精确 `main` SHA、第 10 节文件范围、第 11 节禁止范围和 48 条验收明确批准编码。
5. 实现、48 条验收、全量回归与 Draft 功能 PR。
6. owner 分别批准功能 PR 正式评审和合并。
7. 真实上游激活、凭据、付费调用、目标环境与部署仍需新的独立审批和目标证据。
