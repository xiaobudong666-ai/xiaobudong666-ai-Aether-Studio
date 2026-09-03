# 真实 Provider 私有金丝雀——外部证据采集清单

状态：`COLLECTION_PLAN_ONLY / EVIDENCE_NOT_COLLECTED / EXECUTION_NOT_AUTHORIZED`

编制基线：`main@e43c71166a6e525cad23c47dfd5f30a980d04625`

关联门禁：`docs/approvals/REAL-PROVIDER-PRIVATE-CANARY-EXECUTION-APPROVAL.md`

> 本清单只定义未来由获授权执行人采集和核验外部证据的方法。本文件的编制、评审或合并不授权访问目标、读取或验证凭据、调用 Provider、产生费用、运行 `preflight`、执行 `arm/run`、部署或公开访问。

## 1. 记录规则

- 仓库内只记录 `PRESENT`、`ABSENT`、`INVALID`、`NOT_CHECKED`、稳定原因码、UTC 时间和不可逆的非秘密标识。
- 不记录真实目标地址、用户名、主机名、账户、余额、费用控制台截图、配置路径、文件元数据、文件哈希、键名、API Key、Cookie、Token、Authorization、原始提示或响应。
- 原始证据仅保留在 owner 控制的仓库外受限位置；仓库文档不得提供可反推出秘密位置的路径。
- 任何证据过期、来源不明、结论含糊或无法由第二人复核，均按 `ABSENT` 处理。
- 只有全部强制项为 `PRESENT`，且另行取得精确真实执行授权，才可进入目标侧预检。

## 2. 强制证据清单

| ID | 证据主题 | 外部核验要求 | 仓库允许记录 | 初始状态 | 失败结论 |
|---|---|---|---|---|---|
| EV-01 | 精确代码基线 | 干净检出与 owner 批准 SHA 完全一致 | 批准 SHA、`PRESENT/ABSENT` | `NOT_CHECKED` | `NO-GO` |
| EV-02 | 私有目标控制权 | owner 控制目标，访问路径非公开，Sidecar 无宿主公网端口 | 目标别名 `TARGET-PRIVATE-01`、`PRESENT/ABSENT` | `NOT_CHECKED` | `NO-GO` |
| EV-03 | DeepSeek 费用硬上限 | Provider 侧或等效外部控制面已落实 `CNY 5.00` 总费用硬上限 | `PRESENT/ABSENT`、核验 UTC 时间 | `NOT_CHECKED` | `NO-GO` |
| EV-04 | DeepSeek 账户风险 | 可用余额和账户风险不超过 owner 已批准边界 | `PRESENT/ABSENT` | `NOT_CHECKED` | `NO-GO` |
| EV-05 | Pexels 使用条件 | 允许本次非人物、非品牌、非私人场所的合成主题素材检索 | `PRESENT/ABSENT`、条款核验日期 | `NOT_CHECKED` | `NO-GO` |
| EV-06 | Edge TTS 可用性 | 目标版本支持 Edge 路径与 `zh-CN-XiaoxiaoNeural` | `PRESENT/ABSENT`、版本标识 | `NOT_CHECKED` | `NO-GO` |
| EV-07 | 目标本地文件结构 | 配置、环境、owner cookie、状态文件均在仓库外；普通文件；属主正确；权限不宽于 `0600` | 各文件仅记结构状态，不记路径/哈希/大小/mtime | `NOT_CHECKED` | `NO-GO` |
| EV-08 | 固定 Provider 组合 | 仅 DeepSeek 官方 `deepseek-v4-flash`、Pexels、Edge `zh-CN-XiaoxiaoNeural` | 锁定的非秘密枚举值 | `NOT_CHECKED` | `NO-GO` |
| EV-09 | 无代理与自定义端点 | 不存在代理、自定义 base URL、非空 endpoint 或匿名公共 Provider | `PRESENT/ABSENT` | `NOT_CHECKED` | `NO-GO` |
| EV-10 | 发布与日志关闭 | 自动发布/自动上传关闭；日志为 `WARNING` 或更严格；隐藏配置启用 | `PRESENT/ABSENT` | `NOT_CHECKED` | `NO-GO` |
| EV-11 | 已发布治理绑定 | tenant ID、config version ID、policy hash、project ID 已确定且相互匹配 | 不可逆或非秘密 ID；不得记录账户/路径 | `NOT_CHECKED` | `NO-GO` |
| EV-12 | 单任务预算 | 并发 1、请求 1、输出 1、生成不超过 10 秒、墙钟不超过 900 秒 | 限额值与 `PRESENT/ABSENT` | `NOT_CHECKED` | `NO-GO` |
| EV-13 | 产物路径与权利 | policy 精确限定 `/tasks/`；产物保持 rights-blocked、`adopted=false` | `PRESENT/ABSENT` | `NOT_CHECKED` | `NO-GO` |
| EV-14 | 合成请求 | 中文自然/建筑/物品/抽象主题；无 URL、邮箱、人物、面部、客户、身份信息、上传资产 | 仅记录请求分类与校验状态，不记录原文 | `NOT_CHECKED` | `NO-GO` |
| EV-15 | 唯一执行标识 | approval ID 与 idempotency key 唯一，格式合规，未被使用 | 仅不可逆摘要或唯一非秘密标识 | `NOT_CHECKED` | `NO-GO` |
| EV-16 | 停机责任与窗口 | 指定执行人和紧急停机责任人；15 分钟回滚窗口；禁止不明确结果自动重试 | 角色、窗口与 `PRESENT/ABSENT` | `NOT_CHECKED` | `NO-GO` |
| EV-17 | 证据留存边界 | 仓库外受限位置已准备；只允许去敏字段进入最终证据包 | `PRESENT/ABSENT` | `NOT_CHECKED` | `NO-GO` |
| EV-18 | 独立真实执行授权 | owner 对精确 main SHA、目标别名和一次执行给出明确授权 | 审批状态与审批 ID，不复制秘密 | `ABSENT` | `NO-GO` |

## 3. 证据登记模板

每项只允许使用以下模板，不得附原始截图或秘密内容：

```text
Evidence ID: EV-XX
Status: PRESENT | ABSENT | INVALID | NOT_CHECKED
Checked at UTC: YYYY-MM-DDTHH:MM:SSZ | NOT_CHECKED
Checked by role: OWNER | AUTHORIZED_OPERATOR | INDEPENDENT_REVIEWER
Non-secret reference: <opaque-reference-or-NONE>
Reason code: <stable-reason-code-or-NONE>
Expires at UTC: YYYY-MM-DDTHH:MM:SSZ | NOT_APPLICABLE
Reviewer result: ACCEPTED | REJECTED | NOT_REVIEWED
```

## 4. 禁止采集和禁止提交

- 禁止把 Provider 控制台、目标终端或配置文件的原始截图提交到仓库。
- 禁止复制、粘贴、打印、转述或摘要化任何秘密值。
- 禁止记录目标实际 IP、DNS、SSH 参数、Cookie 文件内容或绝对路径。
- 禁止为“验证 key 是否有效”发起试调用；凭据有效性不属于本清单编制授权。
- 禁止把费用上限的存在误写成已发生付费授权。
- 禁止将 `EV-18=ABSENT` 以任何人工备注覆盖为可执行。

## 5. 汇总结论模板

```text
Baseline: main@e43c71166a6e525cad23c47dfd5f30a980d04625
Evidence present: 0/18
Evidence absent/invalid/not checked: 18/18
Execution approval: ABSENT
Decision: NO-GO
```

本文编制完成时，不代表任何证据已被采集；初始结论固定为 `NO-GO`。
