# IM18–IM20 受治理的私有 Provider 金丝雀——编码审批包

> Status: `DRAFT_FOR_OWNER_APPROVAL`
> Prepared: 2026-08-27 (UTC)
> Authoritative baseline: `main@221540aa2fcb64df4012aa37f8bd017da8e29a9c`
> Depends on: accepted IM15–IM17 Provider activation-readiness controls
> Decision owner: Aether Studio one-person OPC owner
> This document is not real-Provider activation, credential approval, paid-use approval, target deployment or commercial approval.

## 1. 请求批准的决策

本审批包请求批准一个后续编码批次，把已经合并的“可激活控制面”补成一条可在私有目标环境中执行的、一次一任务、可立即关闭并可形成去敏证据的金丝雀路径。实现完成后，仓库、基础 Compose、环境模板和 CI 仍必须默认 `disabled`；所有自动测试仍只能使用 deterministic fake Sidecar。

本批次只包含三个实现单元：

1. **IM18 — 目标本地秘密配置边界**：通过独立 Compose override，把目标主机上的 MoneyPrinter `config.toml` 以只读文件只挂载给 Sidecar；仓库不提供真实配置、不读取秘密内容、不保存秘密摘要，也不把秘密传给 API、Worker、Web 或数据库。
2. **IM19 — Provider 网络与接口隔离**：把无鉴权的固定 Sidecar 从通用应用网络移出；Worker 通过内部控制网络访问，Sidecar 独占单独的外联网络，不发布宿主端口，API/Web/video-use 均不能直接连接 Provider 接口。
3. **IM20 — 私有单任务金丝雀与确定性停机**：增加只读预检、显式 arm/run/disarm 顺序、最多一个请求和最多十秒生成预算、失败即停机、去敏证据清单与可恢复关闭流程；真实执行仍需另行批准。

文档合并、编码授权、功能正式评审、功能合并、真实 Provider/模型选择、凭据、付费调用、私有目标执行和部署必须分别批准。

## 2. 基线事实与已核实缺口

### 2.1 已实现且必须复用

- 项目级生成 API、持久 task/attempt/event、Worker 令牌与租约、取消/重试/恢复及可信产物入库。
- 不可变 Material/AssetVersion、provenance、SHA-256、媒体探测和默认权利阻断。
- operator 环境开关、owner 发布策略和 Worker 短时证明三重门禁。
- Provider 配额预留/释放/结算、持久熔断、owner 紧急停机和服务端权威前端状态。
- 固定 MoneyPrinter Adapter `aether-moneyprinter-v2` 与固定上游 `v1.2.7` / `475f21147f0808f5ffe3f58af9ab794b28a4da2c`。
- Adapter 的 `trust_env=False`、禁止重定向、同源受限产物流、大小/类型校验和稳定错误码。
- 基础 Compose 不发布 MoneyPrinter Sidecar 宿主端口；真实模式默认 `disabled`。

### 2.2 固定上游事实

以下事实针对仓库当前固定的精确上游提交，不代表允许升级：

- 上游 `app/config/config.py` 固定读取 `/MoneyPrinterTurbo/config.toml`；文件不存在时会从 `config.example.toml` 复制。
- 上游 `config.example.toml` 在 `[app]` 中保存 LLM Provider、模型、素材源以及对应 API Key；Pexels/Pixabay 和选定 LLM 可能分别需要凭据。
- 上游 `docker-compose.yml` 通过把源码目录挂载到容器来提供 `config.toml`；Aether 当前镜像没有等价的目标秘密挂载。
- 固定版本的 `app/controllers/v1/video.py` 使用无鉴权 router；因此不能把 Sidecar 接口暴露给 API、Web、宿主公网或其他不需要访问的服务。
- 固定版本会在普通日志中记录任务参数和主题；真实金丝雀只能使用非敏感合成提示，运行配置必须把上游日志级别限制为 `WARNING`，证据采集不得包含原始提示或原始响应。
- 固定版本的默认跨平台自动发布为关闭；金丝雀必须继续明确关闭所有自动发布能力。

权威上游证据：

- `config.py`: https://github.com/harry0703/MoneyPrinterTurbo/blob/475f21147f0808f5ffe3f58af9ab794b28a4da2c/app/config/config.py
- `config.example.toml`: https://github.com/harry0703/MoneyPrinterTurbo/blob/475f21147f0808f5ffe3f58af9ab794b28a4da2c/config.example.toml
- API router: https://github.com/harry0703/MoneyPrinterTurbo/blob/475f21147f0808f5ffe3f58af9ab794b28a4da2c/app/controllers/v1/video.py

### 2.3 当前仓库缺口

- 基础 Compose 没有把目标本地 `config.toml` 只读挂载给 Sidecar；即使把 Aether operator mode 改为 `moneyprinter`，Sidecar 仍没有可用的真实 Provider/素材源配置。
- `moneyprinter-sidecar`、API、Worker 和 video-use 当前共享 `aether-net`；无鉴权 Sidecar 虽未发布宿主端口，但 API 等容器仍可直接访问它。
- 当前运行手册只写“另行配置付费凭据”，没有明确文件权限、挂载目标、网络隔离、单请求预算、停机顺序、秘密清除和证据留存。
- 当前健康证明只证明 Sidecar API 可响应和 Adapter/pin 匹配，不证明目标凭据合法，也不能在零付费调用下证明真实 Provider 可生成。
- Aether 的请求/秒数配额不能代替上游账户的货币硬上限；真实执行前仍需要 Provider 侧预付/余额/硬额度证据。
- 固定上游会处理主题、脚本、搜索词和素材请求；尚无理由允许真实用户数据或身份素材进入第一次金丝雀。

## 3. 权威架构选择

- **基础栈永远默认关闭**：不使用 override 时，Compose 必须与当前行为一致，`AETHER_GENERATION_PROVIDER_MODE=disabled`，不挂载任何真实配置。
- **秘密仅存在于目标主机**：真实 `config.toml` 必须位于 Git 工作树之外、是普通文件、非符号链接、属主可控且权限不宽于 `0600`。
- **只读、单消费者**：真实配置只以 `read_only` bind mount 进入 `moneyprinter-sidecar:/MoneyPrinterTurbo/config.toml`；API、Worker、Web、video-use、数据库和浏览器均不得获得文件或文件内容。
- **不保存秘密摘要**：不得把秘密文件哈希、长度、键名列表、路径、mtime 或内容写入数据库、事件、DTO、浏览器和普通证据；只允许公开 `credentialState=PRESENT|ABSENT|INVALID`。
- **双网络隔离**：Worker 与 Sidecar 共享 `provider-control` 内部网络；Sidecar 独占 `provider-egress` 外联网络；其他服务不连接这两个网络。
- **单向业务边界**：只有 Worker Adapter 能访问 Sidecar；API 继续只做任务、租约、治理、用量和产物入库权威。
- **真实金丝雀只允许一次**：一次授权对应一个租户、一个项目、一个已发布配置版本、一个非敏感合成主题、一个请求、一个输出和最多十秒预算。
- **先停机、后配置、再短时启用**：初始 kill switch 必须为 `DISABLED`；完成预检后才可恢复，结束或任一异常立即重新停机并把 operator mode 恢复为 `disabled`。
- **上游账户硬额度独立存在**：真实执行前必须提供 Provider/素材源账户的货币硬上限或预付余额证据；Aether 不实现支付或账单系统。
- **测试网络封闭**：CI 只使用 deterministic fake config/Sidecar，不读取真实凭据、不解析用户目标文件、不访问公网 Provider、不产生费用。

## 4. IM18——目标本地秘密配置边界

### 4.1 独立 override

新增 `infra/docker/docker-compose.provider-canary.yml`，只在显式追加时生效：

```bash
docker compose \
  --env-file /absolute/private/aether-provider-canary.env \
  -f infra/docker/docker-compose.yml \
  -f infra/docker/docker-compose.provider-canary.yml \
  config --quiet
```

override 只允许：

- 从绝对路径 `MONEYPRINTER_CONFIG_FILE` 绑定目标本地配置；
- 挂载目标固定为 `/MoneyPrinterTurbo/config.toml`；
- `read_only: true`；
- 不把秘密展开为 Compose environment、command、label、healthcheck、build args 或镜像层；
- 不新增宿主端口；
- 不改变固定 MoneyPrinter commit、Dockerfile、依赖或镜像标签。

### 4.2 预检器

新增 `infra/docker/provider-canary.sh`，默认执行只读 `preflight`。必须验证：

- 当前 Git HEAD 与调用参数中的 approved SHA 完全一致且工作树干净；
- 配置文件使用绝对路径、位于仓库外、不是目录/符号链接/设备/FIFO；
- 权限不宽于 `0600`，当前操作用户可读；
- 文件可被 TOML 解析，但输出只包含允许的非秘密状态；
- `log_level="WARNING"`；`upload_post_enabled=false`、`upload_post_auto_upload=false`；
- `enable_redis=false`，不引入外部队列；
- 仅配置审批后确定的一个 LLM Provider、一个模型、一个素材源和一个语音路径；
- 禁止 `g4f`、Pollinations 公共匿名模式、任意代理、任意 base URL、自动发布和用户自定义上传路径；
- Aether 非秘密 policy 的 configVersionId/policyHash、租户和运行 profile 与启动参数一致；
- Provider 账户硬额度证据和素材许可证/来源决策存在，但脚本不读取密钥或调用上游。

预检不得打印 TOML 值、键值对、文件路径、文件哈希、模型提示或任何秘密形态文本。

### 4.3 证明边界

Worker readiness 只可新增去敏状态：

- `credentialState`: `ABSENT | PRESENT | INVALID`
- `networkIsolation`: `ENFORCED | NOT_ENFORCED`
- `canaryProfile`: 固定非秘密标识

这三个字段不得替代 operator/owner/attestation 三重门禁；`PRESENT` 只表示目标文件通过结构预检，不表示凭据正确、余额充足或真实生成成功。

## 5. IM19——Provider 网络与接口隔离

### 5.1 网络拓扑

- `aether-net`：Web、API、Worker、video-use；不包含 MoneyPrinter Sidecar。
- `provider-control`：`internal: true`，只包含 Worker 与 MoneyPrinter Sidecar。
- `provider-egress`：普通 bridge，只包含 MoneyPrinter Sidecar，用于经目标防火墙/DNS策略访问批准的上游域名。
- MoneyPrinter Sidecar 不发布任何宿主端口；API、Web、video-use 不能解析或连接它。
- Worker 继续通过固定服务名访问 Sidecar，不允许运行时 URL 输入或浏览器 URL。

目标环境的域名/IP 出站限制属于后续真实执行证据；Compose 不得伪装成完整的域名级防火墙。

### 5.2 接口与日志

- 固定上游无鉴权 API 只能存在于 `provider-control` 内部网络。
- 旧 Aether `/moneyprinter/*` 路由继续稳定 410；不得增加诊断代理或临时旁路。
- API 容器内对 `moneyprinter-sidecar:8080` 的连接必须失败；Worker 在 fake 集成测试中必须成功连接。
- Sidecar 普通日志级别限制为 WARNING；证据只记录容器健康、稳定错误码、Aether task/attempt/event IDs 和去敏时间戳。
- Docker 日志不得上传真实 `config.toml`、原始 Provider body、完整提示词、请求头、URL 查询、上游任务文件或 Provider 控制台截图中的秘密。

## 6. IM20——私有单任务金丝雀与确定性停机

### 6.1 执行前条件

真实 `arm` 或 `run` 不属于本编码批次授权。后续每次真实执行必须另行提交精确审批，至少包含：

- 已合并的精确 `main` SHA 和目标主机标识；
- 一个确定的 LLM Provider、模型、素材源、语音路径与许可证判断；
- 目标本地配置路径存在性与权限 PASS（不披露路径值）；
- Provider 账户硬消费上限/预付余额、预计单次最大成本与停止阈值；
- 仅私有访问，禁止公网、真实客户数据、个人身份图像/声音和商业发布；
- owner 指定的一个租户、一个项目、一个非敏感合成主题；
- 回滚窗口、证据保留位置和紧急停机责任人。

### 6.2 状态顺序

```text
DISABLED -> PREFLIGHTED -> ARMED -> ONE_TASK_RUNNING
ONE_TASK_RUNNING -> SUCCEEDED | FAILED | UNKNOWN | CANCELED
任一终态 -> DISARMED -> DISABLED
```

- 默认/异常/脚本中断都回到 `DISABLED`。
- `arm` 前 owner kill switch 必须已停机，policy 必须限制为并发 1、月请求 1、月生成秒数不超过 10、输出 1。
- `run` 必须使用唯一 idempotency key，只允许一次 POST；超时或响应不确定进入 `UNKNOWN`，禁止自动第二次提交。
- 产物即使成功，也只能进入 Material + 不可变 AssetVersion，权利保持 blocked；不得自动采纳、时间线写入、渲染或发布。
- `disarm` 必须先触发 owner kill switch，再把 operator mode 恢复为 `disabled`，停止 override 栈，并确认目标秘密未进入容器以外的持久卷或证据包。

### 6.3 证据包

只允许保存：

- 精确 main SHA、Compose 配置摘要中的服务/网络名、固定上游 pin；
- 去敏 readiness 与 `credentialState/networkIsolation/canaryProfile`；
- Aether task/attempt/event/usage/Material/AssetVersion/rights ID；
- 请求开始/结束时间、规范状态、可信 probe 时长、SHA-256、字节数；
- Aether 预留/释放/结算与 Provider 控制台的去敏最终费用/余额确认；
- kill switch、operator disable 和秘密卸载完成时间。

禁止保存秘密、目标配置路径、文件哈希、原始提示、完整上游响应、Provider URL、Cookie/Token/API Key、个人素材或可恢复的账户信息。

## 7. 40 条强制验收

### IM18：秘密配置边界（1–14）

1. 基础 Compose 不追加 override 时保持 `disabled`，不需要真实配置文件并且零 Provider 调用。
2. override 未提供 `MONEYPRINTER_CONFIG_FILE` 时 `config`/preflight 失败且不启动容器。
3. 相对路径、仓库内路径、目录、符号链接、设备和 FIFO 均被拒绝。
4. 权限宽于 `0600` 或当前用户不可读的配置被拒绝。
5. 无效 TOML 被拒绝，输出不包含解析片段或文件路径。
6. 配置只读挂载到 Sidecar 固定目标；容器内写入失败。
7. 配置不进入镜像层、Compose environment、command、label、healthcheck、build args 或 Git diff。
8. API、Worker、Web、video-use 均看不到配置文件或任何 Provider key。
9. 数据库、事件、DTO、浏览器、日志和证据中不出现秘密文件哈希、路径、mtime、长度或键值。
10. `credentialState=PRESENT` 只在结构预检通过时出现；缺失/异常为稳定去敏状态并保持 disabled。
11. `log_level` 不是 WARNING 或更严格时拒绝；上传/自动发布任一开启时拒绝。
12. g4f、匿名公共 Provider、代理、任意 base URL 或多个 LLM/素材源组合被拒绝。
13. configVersionId、policyHash、tenant/profile 任一不匹配时 Worker 证明无效且零上游调用。
14. CI 只使用临时 fake TOML；测试进程不读取开发者或目标主机真实文件。

### IM19：网络与接口隔离（15–26）

15. MoneyPrinter Sidecar 不在 `aether-net`，且无宿主端口。
16. `provider-control` 为 internal，只包含 Worker 与 MoneyPrinter Sidecar。
17. `provider-egress` 只包含 MoneyPrinter Sidecar；Worker/API/Web/video-use 不获得 Provider 公网路径。
18. API 容器不能解析或连接 `moneyprinter-sidecar:8080`。
19. Web 与 video-use 容器不能解析或连接 Sidecar。
20. Worker 在 deterministic fake 集成中可通过 provider-control 访问 Sidecar。
21. Worker Adapter 继续 `trust_env=False`、`follow_redirects=False`，环境代理不能改变来源。
22. 旧 `/moneyprinter/health`、`/capabilities`、`/generate`、`/status` 继续稳定 410。
23. 不新增 Nginx 路由、API 诊断代理、宿主端口或第二个 Provider。
24. Sidecar 警告/错误日志经去敏检查，不包含真实 key、Authorization、Cookie、原始 body 或完整提示。
25. 网络或 Sidecar 健康失败时 readiness 关闭，任务不创建或不 claim，配额不被错误结算。
26. Compose 网络结构在 Linux CI 与静态配置检查中均可证明，且基础健康栈/渲染回归不受影响。

### IM20：单任务金丝雀与停机（27–40）

27. `provider-canary.sh` 默认只运行 preflight，任何真实 arm/run 都要求显式子命令和审批 SHA。
28. HEAD 不匹配、工作树不干净、非 owner、目标非私有或审批标识缺失时拒绝 arm。
29. arm 前 kill switch 必须为 DISABLED；非停机状态拒绝。
30. policy 不是并发 1、月请求 1、月生成秒数 1–10、输出 1 时拒绝。
31. Provider 账户硬额度/预付余额和素材许可证证据缺失时拒绝。
32. run 只接受一个租户、项目、非敏感合成主题和唯一 idempotency key。
33. 单次授权最多一个 create 和一个上游 POST；429/5xx 有界处理，模糊提交不得重放。
34. 成功只创建一组治理产物和一次 SETTLED；重复完成不重复素材或用量。
35. 失败/取消/未提交按既有规则 RELEASED；UNKNOWN 保留证据并立即停机。
36. 任一成功产物仍为 rights-blocked、`adopted=false`，零自动时间线/渲染/发布。
37. SIGINT、脚本异常、健康失败、熔断、超时或预算触发均执行 fail-closed disarm。
38. disarm 后 kill switch 为 DISABLED、operator mode 为 disabled、override 栈停止且秘密只读挂载消失。
39. 证据包通过秘密/提示/路径扫描，只包含第 6.3 节允许字段。
40. 全量 API、Worker、Web、contracts、editor、video-use、Playwright、Docker、FFmpeg、真实渲染和生产浏览器回归通过；CI 零真实 Provider egress、零凭据、零费用。

## 8. 允许的编码文件范围

只有在 owner 对精确 `main` SHA 明确批准编码后，才允许修改或新增：

- `.gitignore`
- `.env.example`
- `infra/docker/.env.example`
- `infra/docker/docker-compose.yml`
- `infra/docker/docker-compose.provider-canary.yml`
- `infra/docker/provider-canary.sh`
- `infra/docker/provider-canary-smoke.py`
- `docs/PRODUCTION_DEPLOYMENT.md`
- `apps/api/app/schemas.py`
- `apps/api/app/main.py`
- `apps/api/test_generation_tasks.py`
- `apps/worker/app/main.py`
- `apps/worker/app/generation_queue.py`
- `apps/worker/test_generation_tasks.py`
- `apps/worker/test_moneyprinter.py`
- `.github/workflows/ci.yml`，仅允许加入 fake canary/Compose 网络/秘密扫描测试，不得改变发布或部署行为
- 与该功能 PR 验证事实对应的 IM18–IM20 evidence 文档

若实现证明需要修改 MoneyPrinter 固定上游、`infra/docker/moneyprinter.Dockerfile`、依赖/锁文件、Nginx、业务模型/迁移、Web UI、真实目标文件或清单之外的功能文件，必须停止并重新申请扩大范围。

## 9. 明确禁止范围

- 在任何已提交文件、基础 Compose 或模板中把真实 Provider 默认设为 `moneyprinter/enabled/active`。
- 提交、复制、读取、打印、上传、摘要化或持久化真实 Provider/素材源 API Key、Cookie、Token、账户、余额明细或目标配置文件。
- 在本编码/CI/正式评审阶段进行真实 Provider、模型、素材源请求或付费调用。
- 在 CI 中访问公网 Provider，或把真实 target config 注入 GitHub Secrets/Actions。
- MoneyPrinterTurbo 上游版本/commit、Dockerfile、依赖、锁文件、基础镜像或 API 合同升级。
- 通过补丁修改固定上游源代码、恢复 API 直连旁路、暴露 Sidecar 端口或新增 Provider 代理。
- 新队列、外部对象存储、秘密管理服务、支付/账单系统、第二个 Provider 或公网控制面。
- 数字人、声音克隆、换脸/换人、换装、背景替换、身份素材或真实客户数据。
- 自动权利允许、自动采纳、自动母版、自动时间线写入、自动渲染或自动发布。
- 真实 `arm/run`、目标部署、公开访问、商业运营或把第一次金丝雀当作生产验收。

## 10. 门禁顺序

1. 本文档 Draft PR 的 CI、JSON、范围审计与自检。
2. owner 批准文档转正式评审。
3. owner 批准文档合并。
4. owner 对新的精确 `main` SHA、第 8 节文件范围、第 9 节禁止范围和 40 条验收明确批准编码。
5. 实现、40 条 fake-only 验收、全量回归与 Draft 功能 PR。
6. owner 分别批准功能 PR 正式评审和合并。
7. owner 选择一个精确真实 LLM Provider/模型、素材源与语音路径，并提供许可证、硬费用上限和私有目标证据。
8. owner 对精确 main SHA、目标主机、一次请求预算、凭据挂载、真实付费调用和回滚窗口作新的独立授权。
9. 私有金丝雀执行、证据评审、是否扩大到有限试用、部署和商业运营继续分别审批。

## 11. 建议的编码授权语句

文档合并后，只有 owner 使用 materially equivalent 的精确授权，才可开始编码：

> 批准以新的精确 `main@<SHA>` 为基线，按《IM18–IM20 受治理的私有 Provider 金丝雀编码审批包》实施功能编码。仅限第8节文件范围，实现目标本地只读秘密挂载、Provider 网络隔离、去敏预检、单任务预算、确定性停机和 fake-only CI；必须完成40条强制验收和全量回归。不得接入或读取真实凭据、访问真实 Provider、产生付费调用、升级固定上游/依赖、执行真实 arm/run、部署或公开访问。校验通过后允许提交、推送并创建 Draft 功能 PR；正式评审、合并、Provider/模型选择、凭据、真实金丝雀、付费使用和部署另行批准。
