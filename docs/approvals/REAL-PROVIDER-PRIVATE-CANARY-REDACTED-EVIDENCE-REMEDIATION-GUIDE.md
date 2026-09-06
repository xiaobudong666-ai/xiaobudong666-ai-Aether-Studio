# 真实 Provider 私有金丝雀——剩余去敏证据补证指引

状态：`GUIDANCE_ONLY / EVIDENCE_UNCHANGED / PREFLIGHT_NOT_AUTHORIZED / NO-GO`

执行代码基线：`main@0d7275836abfef26db7180076b23529b4f974f26`

本次文档编制基线：`main@845ab5d56757b20396099f6d6dea03ef11d833fa`

关联登记：`docs/evidence/REAL-PROVIDER-PRIVATE-CANARY-REDACTED-EVIDENCE-REGISTER.md`

> 本指引只定义未来由已获授权真人在受控环境中补齐去敏证据的方法。编制、评审或合并本文不授权连接私有目标或 Provider 控制台，不授权读取、验证或回传凭据及余额，不授权运行 `preflight`、`arm`、`run`，也不授权 Provider 调用、付费、部署或公开访问。

## 1. 双基线口径

- 执行代码基线固定为 `0d7275836abfef26db7180076b23529b4f974f26`，未来预检只能从该提交的干净检出开始。
- 治理文档基线标识审批、清单、手册和登记材料的版本，不得作为执行脚本的 `--approved-sha`。
- 从执行代码基线到 `845ab5d56757b20396099f6d6dea03ef11d833fa`，`.env.example`、`.github/workflows/ci.yml`、`.gitignore`、`apps/` 与 `infra/` 无差异；后续任一实现路径发生变化，EV-01 自动失效并回到 `NOT_CHECKED / NO-GO`。
- 当前登记结果继续为 `PRESENT 5 / ABSENT 3 / INVALID 0 / NOT_CHECKED 9`；本文不改变任何证据状态。

## 2. 统一采集规则

1. 只有 owner 明确指定的 `AUTHORIZED_OPERATOR` 可以在受控环境核验外部事实；`INDEPENDENT_REVIEWER` 只复核去敏结论。
2. 原始证据只能保存在 owner 控制的仓库外受限位置，不得上传仓库、聊天、工单、CI 或公共存储。
3. 仓库内每项只允许记录 Evidence ID、状态、稳定原因码、UTC 时间、角色和不透明非秘密引用。
4. 不得记录目标地址、主机名、账号、余额、秘密路径、文件哈希、键名、Cookie、Token、API Key、Authorization、请求原文、响应原文或截图。
5. 不得通过试调用判断凭据有效性；不得因核验费用上限而产生费用。
6. 证据来源不明、已过期、无法独立复核、需要泄密才能解释或结果含糊时，统一记录为 `INVALID` 或 `ABSENT`，并保持 `NO-GO`。

## 3. 剩余证据补证矩阵

| ID | 当前状态 | 未来获授权真人的核验目标 | 仓库允许记录 | `PRESENT` 条件 | 稳定失败原因码 |
|---|---|---|---|---|---|
| EV-02 | `NOT_CHECKED` | 确认目标由 owner 控制、访问路径私有且 Sidecar 无宿主公网端口 | `TARGET-PRIVATE-01`、角色、UTC、状态 | 控制权、私有访问和无公网暴露均被独立复核 | `TARGET_CONTROL_UNPROVEN`、`PUBLIC_EXPOSURE_DETECTED` |
| EV-03 | `NOT_CHECKED` | 在 Provider 或等效外部控制面确认总费用硬上限为 `CNY 5.00` | 不透明引用、UTC、状态；不记录余额或控制台内容 | 硬上限已生效且不依赖 Aether 内部软配额 | `HARD_BUDGET_NOT_ENFORCED` |
| EV-04 | `NOT_CHECKED` | 由 owner 判断可用余额及账户风险未超过已批准边界 | 不透明引用、UTC、状态；不记录金额 | owner 明确接受风险且无需披露余额 | `ACCOUNT_RISK_NOT_ACCEPTED` |
| EV-06 | `NOT_CHECKED` | 核对目标固定版本支持 Edge 路径与 `zh-CN-XiaoxiaoNeural` | 非秘密版本标识或不透明引用、UTC、状态 | 目标版本与固定语音均被确认可用 | `TARGET_TTS_VERSION_UNCONFIRMED` |
| EV-07 | `NOT_CHECKED` | 确认四类目标本地文件在仓库外、为普通文件、非符号链接、属主正确且权限不宽于 `0600` | 四类结构结论、UTC、状态；不记录路径、哈希、大小或时间戳 | 每一类文件结构均合规 | `LOCAL_FILE_STRUCTURE_INVALID`、`LOCAL_FILE_PERMISSION_TOO_WIDE` |
| EV-09 | `NOT_CHECKED` | 确认配置不存在代理、自定义 base URL、非空 endpoint 或匿名公共 Provider | UTC、状态、不透明引用 | 四类禁止配置均不存在 | `CUSTOM_ENDPOINT_PRESENT`、`PROXY_CONFIGURATION_PRESENT` |
| EV-10 | `NOT_CHECKED` | 确认自动发布和自动上传关闭、日志级别不低于 `WARNING`、隐藏配置启用 | 四项布尔结论、UTC、状态 | 四项边界全部满足 | `PUBLISH_OR_UPLOAD_ENABLED`、`LOGGING_BOUNDARY_INVALID` |
| EV-11 | `NOT_CHECKED` | 确认 tenant、config version、policy hash、project 四项发布绑定相互匹配 | 不可逆或非秘密 ID、UTC、状态 | 四项已发布且形成唯一一致绑定 | `GOVERNANCE_BINDING_MISMATCH` |
| EV-14 | `ABSENT` | 准备一个中文自然、建筑、物品或抽象合成主题并离线完成分类校验 | 请求分类、校验状态、UTC；不记录请求原文 | 无 URL、邮箱、人物、面部、客户、身份信息或上传资产 | `SYNTHETIC_REQUEST_NOT_PREPARED`、`REQUEST_CLASSIFICATION_REJECTED` |
| EV-15 | `ABSENT` | 发行唯一 approval ID 与 idempotency key，确认格式合规且从未使用 | 不可逆摘要或唯一非秘密标识、UTC、状态 | 两个标识唯一、未使用且与一次执行绑定 | `EXECUTION_IDENTIFIER_NOT_ISSUED`、`EXECUTION_IDENTIFIER_REUSED` |
| EV-16 | `ABSENT` | 指定执行人、独立复核人和紧急停机责任人，锁定 900 秒墙钟与 15 分钟回滚窗口 | 角色、不透明窗口引用、UTC、状态 | 三个责任角色明确，窗口锁定且书面确认禁止不明确结果自动重试 | `STOP_ROLE_NOT_ASSIGNED`、`ROLLBACK_WINDOW_NOT_LOCKED` |
| EV-17 | `NOT_CHECKED` | 确认仓库外受限证据位置及访问、保留、删除边界已准备 | 不透明引用、UTC、状态；不记录真实位置 | 位置受限且只有去敏字段允许进入仓库 | `RESTRICTED_EVIDENCE_LOCATION_UNPROVEN` |

## 4. 分阶段顺序

### 阶段 A：无需接触目标或 Provider 的准备

在获得对应文档工作授权后，可先完成 EV-14、EV-15、EV-16 的非秘密准备。阶段 A 不得连接目标或 Provider，不得验证凭据，不得执行任何运行命令。

### 阶段 B：Provider 控制面人工核验

EV-03、EV-04 只能由已获授权真人在受控环境核验。核验不得复制或回传余额、账户、控制台截图或秘密值，不得发起测试请求。任一项不能明确证明即停止。

### 阶段 C：私有目标人工核验

EV-02、EV-06、EV-07、EV-09、EV-10、EV-11、EV-17 只能由已获授权真人在目标本地或 owner 受控证据环境核验。不得把真实地址、路径或配置内容带出目标。

### 阶段 D：独立复核

独立复核人只接收去敏登记字段，逐项给出 `ACCEPTED` 或 `REJECTED`。任何一项未接受，登记和执行前验收表均保持 `NO-GO`。

## 5. 单项去敏回填模板

```text
Evidence ID: EV-XX
Status: PRESENT | ABSENT | INVALID | NOT_CHECKED
Checked at UTC: YYYY-MM-DDTHH:MM:SSZ | NOT_CHECKED
Checked by role: OWNER | AUTHORIZED_OPERATOR | INDEPENDENT_REVIEWER
Non-secret reference: <OPAQUE_REFERENCE_OR_NONE>
Reason code: <STABLE_REASON_CODE>
Expires at UTC: YYYY-MM-DDTHH:MM:SSZ | NOT_APPLICABLE
Reviewer result: ACCEPTED | REJECTED | NOT_REVIEWED
```

回填前必须再次检查字段值不含秘密、路径、账户、余额、IP、DNS、主机名、请求原文、响应原文或可逆定位信息。

## 6. 完成定义与后续门禁

- EV-01 至 EV-17 全部为 `PRESENT` 且全部经独立复核接受，才允许另行申请一次只读 `preflight` 授权。
- 补证完成不自动授权 `preflight`；预检通过也不自动授权 `arm` 或 `run`。
- EV-18 的一次真实执行授权必须在预检完成、去敏结果独立复核通过后另行申请。
- 任一阶段出现污染、权限不明、边界不一致或需要扩大范围时立即停止，结论保持 `NO-GO`。

## 7. 当前记录

```text
Execution code baseline aligned: YES
Target connected: NO
Provider console accessed: NO
Credentials or balances read: NO
Preflight executed: NO
arm/run executed: NO
Provider called: NO
Paid use: NO
Deployment or public access: NO
Decision: NO-GO
```
