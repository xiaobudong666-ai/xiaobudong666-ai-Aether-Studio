# 真实 Provider 私有单任务金丝雀——选择记录与执行授权包

状态：`CONFIGURATION_LOCKED / EXECUTION_NOT_AUTHORIZED`

执行代码基线：`main@0d7275836abfef26db7180076b23529b4f974f26`

本次文档对齐基线：`main@845ab5d56757b20396099f6d6dea03ef11d833fa`

关联实现：`docs/approvals/IM18-IM20-GOVERNED-PRIVATE-PROVIDER-CANARY-CODING-APPROVAL.md`

> 本文记录 owner 已批准的候选配置，并定义下一道真实执行授权门禁。本文的编制、评审或合并均不构成凭据操作、Provider 调用、付费、`arm`、`run`、部署、公开访问或商业运营授权。

## 1. 已批准的配置选择

owner 已批准以下唯一候选组合：

| 项目 | 锁定值 |
|---|---|
| 精确代码基线 | `main@0d7275836abfef26db7180076b23529b4f974f26` |
| LLM Provider | DeepSeek 官方 API |
| 模型 | `deepseek-v4-flash` |
| 素材源 | Pexels |
| 语音路径 | Microsoft Edge TTS |
| 语音 | `zh-CN-XiaoxiaoNeural` |
| Canary profile | `private-one-task-v1` |
| 并发/请求/输出 | `1 / 1 / 1` |
| 生成时长上限 | `10` 秒 |
| Provider 总费用硬上限 | `CNY 5.00`；必须先在 Provider 侧或等效外部控制面落实 |
| 目标 | owner 已确认的私有目标 `TARGET-PRIVATE-01`；实际地址不得写入仓库或证据 |

Pexels 素材仅允许自然、建筑、物品或抽象场景。首次金丝雀不得检索或采用可识别人物、面部、商标、品牌、车牌、私人场所或其他需额外授权的内容。

## 2. 真人能力边界

产品后续可以处理真人照片、真人视频、数字人、换脸/换人、真人声音或声音克隆，但这些能力不属于首次 Provider 金丝雀，不能因本配置选择而自动开启。

真人路径至少需要一份独立审批包，并同时满足：

1. 素材主体为 owner 本人，或已取得可核验的书面知情同意、肖像权、声音权及约定用途授权；
2. 授权覆盖输入、AI 处理、输出、保存期限、展示范围、撤回和删除方式；
3. 禁止未成年人、公众人物、第三方客户、员工或其他无法核验授权的身份素材；
4. 禁止冒充、欺诈、色情、骚扰、政治误导、规避平台披露或使观众误认真实事件；
5. 对换脸、声音克隆及高度写实数字人设置显著 AI 标识、溯源记录、访问控制与人工发布审批；
6. 原素材、面部特征、声纹、中间文件和输出均按敏感个人信息管理，不进入日志、代码仓库或通用测试证据；
7. 首次真人测试仍须单人、单任务、单输出、私有环境，并在执行前另行批准具体人物、素材、用途、保留期和删除证明。

因此，本次金丝雀请求必须保持合成、非身份化和无输入资产；通过该金丝雀只能证明基础 Provider 链路，不能证明真人能力已验收或可商用。

## 3. 当前仍被禁止的动作

- 不得查找、读取、复制、写入、打印、上传、摘要化或验证任何真实 API Key、Cookie、Token、余额或目标配置内容；
- 不得连接 `TARGET-PRIVATE-01`，不得运行目标探测、Compose、SSH、健康检查或只读 `preflight`；
- 不得访问 DeepSeek、Pexels 或 Edge TTS 的真实端点；
- 不得设置 `AETHER_CANARY_REAL_EXECUTION_APPROVED=true`；
- 不得执行 `provider-canary.sh arm`、`run` 或 `disarm`；
- 不得产生任何费用，不得部署、开放端口、公开访问或扩大到真人素材；
- 不得把目标地址、凭据路径、账户资料或秘密证据提交到仓库。

## 4. 执行前必须补齐的外部证据

以下证据仅记录为 `PRESENT/ABSENT` 或不可逆摘要；秘密值与账户详情不得进入仓库：

1. DeepSeek 账户硬费用上限已落实，且可用余额不超过 owner 允许风险；
2. Pexels API 使用条件已核对，素材检索规则已限定为无可识别人物/品牌；
3. Edge TTS 路径及 `zh-CN-XiaoxiaoNeural` 在目标版本中的可用性已核对；
4. `TARGET-PRIVATE-01` 的目标主机身份、owner 控制权、私有访问方式及无公网 Sidecar 暴露证据；
5. 目标外部 `config.toml`、环境文件、owner cookie 文件和状态文件的绝对路径、owner UID 与 `0600` 权限结构证明；
6. 已发布的 tenant ID、Provider 配置版本 ID、policy hash、project ID；
7. 一个无 URL、邮箱、人物、面部、客户或身份信息的中文合成主题请求；
8. 唯一 approval ID、唯一 idempotency key、最长 `900` 秒墙钟预算和 `15` 分钟回滚窗口；
9. 执行人确认提交结果不明确时绝不自动重试，并能立即执行 fail-closed disarm。

任何一项缺失均为 `NO-GO`。

## 5. 首次请求固定边界

首次请求必须符合 `infra/docker/provider-canary-smoke.py validate-request`，并固定为：

- `durationMs`: `10000` 或更短；
- `outputCount`: `1`；
- `inputAssetVersionIds`: `[]`；
- `voiceName`: `zh-CN-XiaoxiaoNeural`；
- `videoSubject`: 仅合成、非身份化的自然/抽象中文主题；
- 不包含真人、face、person、customer、client、声音克隆、换脸/换人、私人信息、第三方作品链接或上传资产。

## 6. 分阶段执行顺序

只有 owner 完成第 7 节的独立执行授权后，才能按以下顺序进行，且每一步失败立即停止：

1. 从精确批准 SHA 的干净检出开始；
2. 只运行去敏结构预检；
3. 报告预检结果并确认 Provider 硬费用上限仍有效；
4. 显式执行一次 `arm`；
5. 显式执行一次 `run`，最多一个 POST；
6. 无论成功、失败、超时或未知，立即 fail-closed `disarm`；
7. 生成去敏证据，核对零秘密泄露、单请求、单输出、费用和停机状态；
8. 单独申请证据评审。不得自动重试、继续第二个任务、部署或进入真人测试。

## 7. 尚未批准的真实执行授权模板

只有 owner 在外部证据齐全后使用以下文字或 materially equivalent 的精确授权，才构成一次真实执行批准：

> 批准以 `main@0d7275836abfef26db7180076b23529b4f974f26` 为唯一基线，在 owner 已确认的私有目标 `TARGET-PRIVATE-01` 上，按《真实 Provider 私有单任务金丝雀——选择记录与执行授权包》执行一次受治理金丝雀。仅允许 DeepSeek 官方 `deepseek-v4-flash`、Pexels、Edge `zh-CN-XiaoxiaoNeural`，一个无真人合成主题、一个请求、一个输出、最长十秒，总费用硬上限人民币 5 元，墙钟上限 900 秒，回滚窗口 15 分钟。批准目标本地只读凭据挂载、去敏 preflight、一次 arm、一次 run 和强制 disarm；不得读取或回传秘密值，不得自动重试，不得使用真人或身份素材，不得部署、公开访问、扩大试用或商业运营。执行完成后只提交去敏证据，后续步骤另行批准。

当前该模板状态为：`NOT_APPROVED`。

## 8. 停止条件

出现以下任一情况立即停止并执行或人工确认 fail-closed disarm：SHA/工作树不符、目标无法证明为私有、权限过宽、配置含代理或自定义 base URL、硬费用上限未落实、Provider/模型/素材/语音不符、请求含真人或身份素材、一次 POST 状态不明确、外部响应异常、日志可能含秘密、超过预算或墙钟、停机无法证明。

## 9. 本次审批记录

owner 已批准第 1 节候选配置，确认 `TARGET-PRIVATE-01` 为私有目标，并仅授权配置锁定及本文编制。owner 明确未批准凭据操作、真实调用、付费、`arm/run`、部署或公开访问。

执行代码基线固定为 `0d7275836abfef26db7180076b23529b4f974f26`。从该提交到本次文档对齐基线 `845ab5d56757b20396099f6d6dea03ef11d833fa`，`.env.example`、`.github/workflows/ci.yml`、`.gitignore`、`apps/` 与 `infra/` 无差异；其间提交仅增加本组治理文档。后续预检必须使用执行代码基线，文档提交 SHA 仅用于标识治理材料版本，二者不得混用。

本文没有记录任何真实凭据、目标地址、账户、余额、Cookie、Token 或秘密文件内容。
