# 真实 Provider 私有金丝雀——去敏角色正式任命记录

状态：`ROLES_APPOINTED / REVIEW_NOT_PERFORMED / EVIDENCE_UNCHANGED / PREFLIGHT_NOT_AUTHORIZED / NO-GO`

任命记录 ID：`APPT-REAL-CANARY-20260910-A01`

治理基线：`main@9cc0f7e18ab10a4e7f31fd5a94b97aaf4633e441`

记录时间：`2026-09-10T18:34:56Z`

关联审批包：`docs/approvals/REAL-PROVIDER-PRIVATE-CANARY-OPC-PHASE-BC-READONLY-EVIDENCE-PACK.md`

关联 EV-16 记录：`docs/evidence/REAL-PROVIDER-PRIVATE-CANARY-EV16-STOP-ROLE-WINDOW-CONFIRMATION.md`

关联证据登记：`docs/evidence/REAL-PROVIDER-PRIVATE-CANARY-REDACTED-EVIDENCE-REGISTER.md`

> 本记录仅固化 owner 已明确作出的角色任命。它不记录或证明真人身份，不构成独立复核结论，不改变 EV-16 或其他证据状态，也不授权阶段 B/C 核验、只读 `preflight` 或真实执行。

## 1. Owner 任命决定

Owner 已在精确治理基线下作出以下任命：

| 责任槽位 | 任命对象 | 任命状态 | 当前权限边界 |
|---|---|---|---|
| `AUTHORIZED_OPERATOR` | Jackie | `APPOINTED` | 仅确认角色；阶段 B/C 核验尚未授权 |
| `EMERGENCY_STOP_OWNER` | Jackie | `APPOINTED` | 仅确认角色；未授权目标连接、停机操作或真实执行 |
| `INDEPENDENT_REVIEWER` | `REVIEWER-01` | `APPOINTED / NOT_REVIEWED` | 只允许未来在另行授权后接收去敏字段并独立复核 |

本次任命不把任何角色的未来职责视为已经履行。

## 2. `REVIEWER-01` 去敏身份声明

- Owner 确认 `REVIEWER-01` 在仓库外受限记录中唯一对应一名非 Jackie 的真人。
- 仓库、PR、CI、聊天记录和证据登记均不得写入该真人的姓名、联系方式、身份证明或可逆身份映射。
- Codex、自动化流程、模型、机器人和 Jackie 均不得冒充 `REVIEWER-01`，不得替其签署 `ACCEPTED` 或 `REJECTED`。
- `REVIEWER-01` 的任命不等于其已经接收材料、完成复核或接受任何证据。
- 未来复核只允许接收规定的去敏字段，不需要且不得接触凭据、余额、真实地址、秘密路径、控制台原文、目标配置原文或原始证据。

## 3. 本次任命的明确非授权项

本记录不授权以下任何行为：

1. 阶段 B Provider 控制面人工核验。
2. 阶段 C 私有目标人工核验或连接 `TARGET-PRIVATE-01`。
3. 读取、验证、复制、回传或测试任何凭据、余额、账户或付款资料。
4. 运行真实 `preflight`、`arm` 或 `run`。
5. 调用 Provider、产生费用、购买服务或扩大任务预算。
6. 修改目标配置、部署、发布、上传或启用公开访问。
7. 签发或激活 EV-15 的一次性执行标识。
8. 将角色任命解释为 EV-16 已完成独立复核。
9. 修改任何 Evidence ID 的状态、原因码、时间、引用或汇总。
10. 将整体结论从 `NO-GO` 提升为任何形式的 `GO`。

## 4. EV-16 与证据状态隔离

本次只新增任命记录，不修改既有 EV-16 文件或证据登记。

```text
Authorized operator appointment recorded: YES
Emergency stop owner appointment recorded: YES
Independent reviewer appointment recorded: YES
Independent reviewer identity disclosed: NO
Independent review performed: NO
Independent reviewer result: NOT_REVIEWED
EV-16 evidence status changed: NO
EV-16 current registered status: ABSENT
EV-16 current reason code: STOP_ROLE_NOT_ASSIGNED
Other evidence status changed: NO
```

`STOP_ROLE_NOT_ASSIGNED` 暂按既有登记原样保留。只有另行获批的去敏证据回填能够证明任命已被三方确认、时间边界被接受且独立复核完成后，才可申请更新 EV-16；本记录自身不得作为自动提升依据。

## 5. 后续门禁

1. 如需开展阶段 B，owner 必须对 EV-03、EV-04 的精确只读人工核验范围另行授权。
2. 如需开展阶段 C，owner 必须对 EV-02、EV-06、EV-07、EV-09、EV-10、EV-11、EV-17 的精确只读人工核验范围另行授权。
3. 各项核验只能由已任命角色在其权限范围内实施；`REVIEWER-01` 只接收去敏结果。
4. 任何事实含糊、需要泄密、无法独立复核、已过期或超出范围时立即停止，并保持 `ABSENT`、`INVALID` 或 `NOT_CHECKED`。
5. EV-01 至 EV-17 未全部为 `PRESENT` 且未全部由 `REVIEWER-01` 独立接受前，不得申请真实 `preflight`。
6. 即使未来只读 `preflight` 通过，`arm/run` 仍必须另行取得一次性精确授权。

## 6. 完成记录

```text
Governance baseline exact: YES
Role appointment record prepared: YES
Jackie appointed as AUTHORIZED_OPERATOR: YES
Jackie appointed as EMERGENCY_STOP_OWNER: YES
REVIEWER-01 appointed as INDEPENDENT_REVIEWER: YES
REVIEWER-01 mapped outside repository to a non-Jackie human: OWNER_CONFIRMED
Identity or contact data stored in repository: NO
Evidence status changed: NO
Stage B/C verification authorized: NO
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
