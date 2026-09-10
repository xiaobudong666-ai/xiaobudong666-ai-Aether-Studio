# 真实 Provider 私有金丝雀——一人 OPC 角色分配与阶段 B/C 只读人工补证审批包

状态：`DRAFT_PREPARED / PROPOSED_ROLES_ONLY / EVIDENCE_UNCHANGED / PREFLIGHT_NOT_AUTHORIZED / NO-GO`

审批包 ID：`PACK-OPC-PHASE-BC-20260910-A01`

文档基线：`main@dd6fc186adc84388c9a0d1aa0dd592ad2521fc2d`

执行代码基线：`main@0d7275836abfef26db7180076b23529b4f974f26`

关联阶段 A 补证包：`PACK-EV14-EV16-20260906-A01`

关联登记：`docs/evidence/REAL-PROVIDER-PRIVATE-CANARY-REDACTED-EVIDENCE-REGISTER.md`

> 本审批包仅供离线评审。它记录一人 OPC 条件下的拟任角色和未来阶段 B/C 的逐项去敏核验清单，不构成角色正式任命、外部事实核验、证据状态提升、只读 `preflight` 或真实执行授权。

## 1. 本次获批编制范围

- 记录 Jackie 拟担任 `AUTHORIZED_OPERATOR` 与 `EMERGENCY_STOP_OWNER`。
- 保持 `INDEPENDENT_REVIEWER` 为 `UNASSIGNED`。
- 整理 EV-02、EV-03、EV-04、EV-06、EV-07、EV-09、EV-10、EV-11、EV-14、EV-15、EV-16、EV-17 的逐项去敏核验清单。
- 仅允许离线文档校验、提交、推送及创建 Draft 文档 PR。
- 不改变登记中任何 Evidence ID 的状态、原因码、时间或引用。

## 2. 一人 OPC 拟任角色草案

| 责任槽位 | 拟任安排 | 当前效力 | 限制 |
|---|---|---|---|
| `OWNER` | Jackie | 已知项目负责人和最终批准人 | 不能以 owner 身份自动签发执行授权或替代独立复核 |
| `AUTHORIZED_OPERATOR` | Jackie | `PROPOSED / NOT_APPOINTED` | 未获阶段 B/C 精确授权前不得访问 Provider 控制面或私有目标 |
| `EMERGENCY_STOP_OWNER` | Jackie | `PROPOSED / NOT_APPOINTED` | 未形成正式角色确认前不得视为 EV-16 证据 |
| `INDEPENDENT_REVIEWER` | `UNASSIGNED` | `UNASSIGNED / NOT_REVIEWED` | Codex、自动化流程和 owner 均不得冒充真人独立复核人或代签 |

角色草案不得写回为正式任命。只有 owner 对角色、核验范围、时间窗口和安全边界另行给出精确批准后，相关人员才可开始对应人工核验。

## 3. 全程统一边界

1. 原始证据只能保存在 owner 控制的仓库外受限位置；不得上传到 GitHub、聊天、CI、工单或公共存储。
2. 仓库内只允许记录 Evidence ID、状态、稳定原因码、UTC 时间、角色及不透明非秘密引用。
3. 禁止记录或回传真实地址、IP、DNS、主机名、账户、余额、秘密路径、文件哈希、键名、Cookie、Token、API Key、Authorization、请求原文、响应原文或控制台截图。
4. 不得通过测试请求判断凭据有效性；不得读取、验证或回传凭据及余额；不得产生费用。
5. 任一事实不明确、无法独立复核、需要泄密才能说明、已过期或超出范围时，立即停止该项核验并维持 `ABSENT`、`INVALID` 或 `NOT_CHECKED`。
6. 阶段 B/C 补证不授权运行真实 `preflight`、`arm` 或 `run`，不授权 Provider 调用、部署、发布或公开访问。
7. `INDEPENDENT_REVIEWER` 未正式指定且未逐项接受之前，整体结论始终为 `NO-GO`。

## 4. 阶段 B——Provider 控制面只读人工补证清单

本节仅定义未来人工核验步骤；当前不得打开、登录或访问 Provider 控制面。

### EV-03——外部硬费用上限

- 当前状态：`NOT_CHECKED / HUMAN_PROVIDER_CONTROL_PLANE_REQUIRED`
- 核验目标：确认外部控制面已生效的总费用硬上限不高于 `CNY 5.00`，且不依赖 Aether 内部软配额。
- 操作人：正式任命后的 `AUTHORIZED_OPERATOR`。
- 允许记录：Evidence ID、UTC、角色、`PRESENT | ABSENT | INVALID`、稳定原因码、不透明非秘密引用。
- 禁止记录：账户、余额、账单、付款方式、控制台内容、截图或可逆定位信息。
- `PRESENT` 条件：硬上限已生效，并由正式任命的 `INDEPENDENT_REVIEWER` 接受去敏结论。
- 停止条件：无法证明硬上限、需要试调用、需要披露金额细节或出现账户风险。
- 稳定失败原因码：`HARD_BUDGET_NOT_ENFORCED`。

### EV-04——账户与余额风险接受

- 当前状态：`NOT_CHECKED / HUMAN_PROVIDER_CONTROL_PLANE_REQUIRED`
- 核验目标：由 owner 判断可用余额及账户风险未超过已批准边界。
- 操作人：正式任命后的 `AUTHORIZED_OPERATOR`；风险接受只能由 `OWNER` 作出。
- 允许记录：owner 是否接受风险的布尔结论、UTC、角色、不透明非秘密引用。
- 禁止记录：余额、金额明细、账户标识、付款资料、控制台内容或截图。
- `PRESENT` 条件：owner 明确接受风险，且独立复核人接受去敏记录。
- 停止条件：余额不足、风险不明确、必须复制控制台内容或需要发起测试请求。
- 稳定失败原因码：`ACCOUNT_RISK_NOT_ACCEPTED`。

## 5. 阶段 C——私有目标只读人工补证清单

本节仅定义未来在 owner 受控环境中的人工核验步骤；当前不得连接 `TARGET-PRIVATE-01`。

### EV-02——目标控制权与私有暴露边界

- 当前状态：`NOT_CHECKED / HUMAN_TARGET_VERIFICATION_REQUIRED`
- 核验目标：确认目标由 owner 控制、访问路径私有，并且 Sidecar 没有宿主公网端口。
- 允许记录：`TARGET-PRIVATE-01`、布尔结论、UTC、角色、稳定原因码。
- `PRESENT` 条件：控制权、私有访问和无公网暴露全部成立，并完成独立复核。
- 停止条件：发现公网暴露、控制权不清或必须记录真实地址。
- 稳定失败原因码：`TARGET_CONTROL_UNPROVEN`、`PUBLIC_EXPOSURE_DETECTED`。

### EV-06——目标固定版本与中文语音支持

- 当前状态：`NOT_CHECKED / TARGET_VERSION_HUMAN_VERIFICATION_REQUIRED`
- 核验目标：确认目标固定版本支持 Edge 路径与 `zh-CN-XiaoxiaoNeural`。
- 允许记录：非秘密版本标识或不透明引用、两个能力的布尔结论、UTC、角色。
- `PRESENT` 条件：目标版本与固定语音均被确认可用，并完成独立复核。
- 停止条件：需要真实生成、Provider 调用、升级目标或修改配置才能判断。
- 稳定失败原因码：`TARGET_TTS_VERSION_UNCONFIRMED`。

### EV-07——四类本地文件结构与权限

- 当前状态：`NOT_CHECKED / HUMAN_TARGET_VERIFICATION_REQUIRED`
- 核验目标：确认四类目标本地文件位于仓库外、均为普通文件、不是符号链接、属主正确且权限不宽于 `0600`。
- 允许记录：四类结构与权限的布尔结论、UTC、角色、不透明非秘密引用。
- 禁止记录：路径、哈希、大小、时间戳、文件名、键名或内容。
- `PRESENT` 条件：四类文件全部合规，并完成独立复核。
- 停止条件：任一文件为符号链接、权限过宽、属主不明或位于仓库内。
- 稳定失败原因码：`LOCAL_FILE_STRUCTURE_INVALID`、`LOCAL_FILE_PERMISSION_TOO_WIDE`。

### EV-09——端点与代理禁止项

- 当前状态：`NOT_CHECKED / TARGET_CONFIGURATION_HUMAN_VERIFICATION_REQUIRED`
- 核验目标：确认不存在代理、自定义 base URL、非空 endpoint 或匿名公共 Provider。
- 允许记录：四类禁止项均不存在的布尔结论、UTC、角色、不透明非秘密引用。
- 禁止记录：代理值、URL、主机名、端点内容或配置原文。
- `PRESENT` 条件：四类禁止配置全部不存在，并完成独立复核。
- 停止条件：任一禁止项存在、结果含糊或需要修改配置。
- 稳定失败原因码：`CUSTOM_ENDPOINT_PRESENT`、`PROXY_CONFIGURATION_PRESENT`。

### EV-10——发布、上传、日志与隐藏配置

- 当前状态：`NOT_CHECKED / TARGET_CONFIGURATION_HUMAN_VERIFICATION_REQUIRED`
- 核验目标：确认自动发布关闭、自动上传关闭、日志级别不低于 `WARNING`、隐藏配置启用。
- 允许记录：四项布尔结论、UTC、角色、不透明非秘密引用。
- `PRESENT` 条件：四项全部满足，并完成独立复核。
- 停止条件：发现发布或上传启用、日志边界过宽、隐藏配置关闭或状态不明确。
- 稳定失败原因码：`PUBLISH_OR_UPLOAD_ENABLED`、`LOGGING_BOUNDARY_INVALID`。

### EV-11——治理发布绑定一致性

- 当前状态：`NOT_CHECKED / TARGET_GOVERNANCE_BINDING_HUMAN_REQUIRED`
- 核验目标：确认 tenant、config version、policy hash、project 四项发布绑定相互匹配。
- 允许记录：不可逆或非秘密 ID、匹配布尔结论、UTC、角色、不透明非秘密引用。
- 禁止记录：租户秘密、配置正文、可逆哈希前镜像或目标定位信息。
- `PRESENT` 条件：四项已发布并形成唯一一致绑定，且完成独立复核。
- 停止条件：任一项缺失、不一致、未发布或只能通过泄密说明。
- 稳定失败原因码：`GOVERNANCE_BINDING_MISMATCH`。

### EV-17——仓库外受限证据位置

- 当前状态：`NOT_CHECKED / HUMAN_RETENTION_LOCATION_VERIFICATION_REQUIRED`
- 核验目标：确认仓库外受限证据位置及其访问、保留、删除边界已经准备。
- 允许记录：位置受限、访问受控、保留规则和删除规则的布尔结论、UTC、角色、不透明非秘密引用。
- 禁止记录：真实位置、路径、账户、访问方式、密钥、截图或原始证据。
- `PRESENT` 条件：受限位置与全生命周期边界全部成立，并完成独立复核。
- 停止条件：位置未准备、访问范围不清、原始证据可能进入仓库或公共存储。
- 稳定失败原因码：`RESTRICTED_EVIDENCE_LOCATION_UNPROVEN`。

## 6. 阶段 A 延续项——EV-14 至 EV-16

以下项目尚未满足提升条件；阶段 B/C 审批不得自动激活它们。

### EV-14——合成请求分类

- 当前状态：`ABSENT / SYNTHETIC_REQUEST_NOT_PREPARED`
- 核验目标：由获授权真人在仓库外受限位置准备一个中文自然、建筑、物品或抽象主题，并完成离线分类。
- 必须排除：URL、邮箱、人物、面部、客户信息、身份信息和上传资产。
- 允许记录：分类结论、UTC、角色、不透明非秘密引用；不得记录请求原文。
- `PRESENT` 条件：分类全部通过，并完成独立复核。
- 稳定失败原因码：`SYNTHETIC_REQUEST_NOT_PREPARED`、`REQUEST_CLASSIFICATION_REJECTED`。

### EV-15——一次性执行标识签发

- 当前状态：`ABSENT / EXECUTION_IDENTIFIER_NOT_ISSUED`
- 核验目标：在未来单次执行获得独立精确授权后，再确认候选 approval ID 与 idempotency key 唯一、未使用且一一绑定。
- 允许记录：唯一非秘密标识或不可逆摘要、UTC、角色、状态。
- 当前限制：已预留候选值不得签发、激活或用于 `preflight`、`arm`、`run`。
- `PRESENT` 条件：标识正式签发、唯一、未使用、绑定单次执行，并完成独立复核。
- 稳定失败原因码：`EXECUTION_IDENTIFIER_NOT_ISSUED`、`EXECUTION_IDENTIFIER_REUSED`。

### EV-16——角色与停机窗口

- 当前状态：`ABSENT / STOP_ROLE_NOT_ASSIGNED`
- 已锁定边界：墙钟 `900` 秒、回滚窗口 `15` 分钟、并发 `1`、请求 `1`、输出 `1`、禁止不明确结果自动重试。
- 当前草案：Jackie 拟任 `AUTHORIZED_OPERATOR` 与 `EMERGENCY_STOP_OWNER`；`INDEPENDENT_REVIEWER` 为 `UNASSIGNED`。
- 当前限制：拟任安排不得视为正式任命；任何自动化或模型不得代签独立复核。
- `PRESENT` 条件：三个责任槽位正式明确、时间边界书面确认，并由独立复核人接受。
- 稳定失败原因码：`STOP_ROLE_NOT_ASSIGNED`、`ROLLBACK_WINDOW_NOT_LOCKED`。

## 7. 单项去敏结果模板

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

回填前必须检查所有字段不包含秘密、路径、账户、余额、IP、DNS、主机名、请求原文、响应原文、控制台内容或可逆定位信息。

## 8. 阶段顺序与停止门禁

1. 先由 owner 正式任命角色并逐项批准阶段 B/C 精确范围。
2. 阶段 B 仅人工核验 EV-03、EV-04；任一项不明确即停止，不进入阶段 C。
3. 阶段 C 仅人工核验 EV-02、EV-06、EV-07、EV-09、EV-10、EV-11、EV-17；任一项不明确即停止。
4. EV-14、EV-15、EV-16 只能分别满足自身条件后申请提升，不随阶段 B/C 自动提升。
5. 正式任命后的独立复核人只接收去敏字段，逐项给出 `ACCEPTED` 或 `REJECTED`。
6. EV-01 至 EV-17 未全部为 `PRESENT` 且未全部独立复核接受前，不得申请真实 `preflight`。
7. 即使未来只读 `preflight` 通过，也不得自动执行 `arm` 或 `run`；真实执行必须另行取得一次性精确授权。

## 9. 本审批包完成记录

```text
Document baseline exact: YES
Proposed role draft recorded: YES
Jackie formally appointed as operator: NO
Jackie formally appointed as emergency stop owner: NO
Independent reviewer assigned: NO
Evidence status changed: NO
Target connected: NO
Provider console accessed: NO
Credentials or balances read or validated: NO
Real preflight executed: NO
arm/run executed: NO
Provider called: NO
Paid use: NO
Deployment or public access: NO
Decision: NO-GO
```
