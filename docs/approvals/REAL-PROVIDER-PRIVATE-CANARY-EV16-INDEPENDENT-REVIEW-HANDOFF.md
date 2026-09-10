# EV-16——去敏独立复核交接包

状态：`HANDOFF_PREPARED / HUMAN_REVIEW_PENDING / EVIDENCE_UNCHANGED / NO-GO`

交接包 ID：`HANDOFF-EV16-20260910-A01`

治理基线：`main@624e26e14dc3d5bd7f51e83f98248abb3b9e366b`

编制时间：`2026-09-10T19:05:45Z`

关联任命记录：`docs/approvals/REAL-PROVIDER-PRIVATE-CANARY-FORMAL-ROLE-APPOINTMENT.md`

关联 EV-16 记录：`docs/evidence/REAL-PROVIDER-PRIVATE-CANARY-EV16-STOP-ROLE-WINDOW-CONFIRMATION.md`

关联证据登记：`docs/evidence/REAL-PROVIDER-PRIVATE-CANARY-REDACTED-EVIDENCE-REGISTER.md`

> 本交接包只向已任命的 `REVIEWER-01` 提供 EV-16 的去敏复核字段。它不是复核结论，不代表材料已交付，不得由 Jackie、Codex、自动化、模型或机器人代填 `ACCEPTED` 或 `REJECTED`，也不改变 EV-16 或其他证据状态。

## 1. 复核主体与职责分离

| 责任槽位 | 已任命对象 | 任命状态 | 本次边界 |
|---|---|---|---|
| `AUTHORIZED_OPERATOR` | Jackie | `APPOINTED` | 仅确认角色；未授权阶段 B/C 或真实执行 |
| `EMERGENCY_STOP_OWNER` | Jackie | `APPOINTED` | 仅确认角色；未授权连接目标或实施停机 |
| `INDEPENDENT_REVIEWER` | `REVIEWER-01` | `APPOINTED / NOT_REVIEWED` | 仅由对应真人在仓库外独立复核本包去敏字段 |

Owner 已确认 `REVIEWER-01` 在仓库外唯一对应一名非 Jackie 的真人。本包不记录该真人的姓名、联系方式、身份证明或可逆身份映射。

## 2. 提交给 `REVIEWER-01` 的去敏事实

### 2.1 角色任命事实

```text
AUTHORIZED_OPERATOR appointed: YES
AUTHORIZED_OPERATOR alias: Jackie
EMERGENCY_STOP_OWNER appointed: YES
EMERGENCY_STOP_OWNER alias: Jackie
INDEPENDENT_REVIEWER appointed: YES
INDEPENDENT_REVIEWER alias: REVIEWER-01
REVIEWER-01 is a non-Jackie human: OWNER_CONFIRMED
Identity disclosure required: NO
```

### 2.2 确定性控制边界

| 控制项 | 锁定值 | 待复核结论 |
|---|---|---|
| 墙钟上限 | `900` 秒 | `PENDING_HUMAN_REVIEW` |
| 回滚窗口 | `15` 分钟 | `PENDING_HUMAN_REVIEW` |
| 并发 | `1` | `PENDING_HUMAN_REVIEW` |
| 请求数 | `1` | `PENDING_HUMAN_REVIEW` |
| 输出数 | `1` | `PENDING_HUMAN_REVIEW` |
| 不明确结果自动重试 | 禁止 | `PENDING_HUMAN_REVIEW` |
| 超时或结果不明确 | 进入 `UNKNOWN` 并停止 | `PENDING_HUMAN_REVIEW` |
| 后续 `arm/run` | 必须另行取得一次性精确授权 | `PENDING_HUMAN_REVIEW` |

## 3. 真人独立复核问题

`REVIEWER-01` 只需根据上方去敏字段独立判断：

1. 三个责任槽位是否已经明确且不存在角色空缺。
2. `INDEPENDENT_REVIEWER` 是否与 Jackie 分离，并由非 Jackie 真人承担。
3. `900` 秒墙钟和 `15` 分钟回滚窗口是否明确且无歧义。
4. 并发、请求数和输出数是否均锁定为 `1`。
5. 不明确结果是否明确禁止自动重试，并要求进入 `UNKNOWN` 后停止。
6. 本包是否没有把角色任命或复核交接解释为真实执行授权。
7. 本包是否足以给出 EV-16 的去敏独立复核意见，而无需接触任何禁止字段。

任一问题无法明确判断时，结果必须为 `REJECTED`；不得猜测、补全或要求披露禁止字段。

## 4. 仓库外交接与返回格式

本包只能由 owner 通过其控制的仓库外受限方式交给 `REVIEWER-01` 对应真人。该真人只返回以下去敏字段：

```text
Evidence ID: EV-16
Handoff ID: HANDOFF-EV16-20260910-A01
Reviewer role: INDEPENDENT_REVIEWER
Reviewer alias: REVIEWER-01
Reviewer result: ACCEPTED | REJECTED
Reviewed at UTC: YYYY-MM-DDTHH:MM:SSZ
Reason code: EV16_REVIEW_ACCEPTED | ROLE_SEPARATION_REJECTED | TIME_BOUNDARY_REJECTED | RETRY_BOUNDARY_REJECTED | INSUFFICIENT_REDACTED_FACTS
Non-secret reference: <OPAQUE_REFERENCE_OR_NONE>
```

不得返回姓名、联系方式、身份证明、签名图像、真实地址、IP、DNS、主机名、账户、余额、凭据、秘密路径、控制台内容、配置原文、截图或原始证据。

## 5. 结果登记门禁

1. 当前 `Reviewer result` 固定为 `NOT_REVIEWED`。
2. 在收到 `REVIEWER-01` 对应真人按第 4 节返回的完整去敏结果前，不得更新现有 EV-16 文件或证据登记。
3. `ACCEPTED` 只表示独立复核人接受本包中的角色与时间边界；它本身不自动把 EV-16 改为 `PRESENT`。
4. 任何 EV-16 状态、原因码、时间或引用的变更都必须另行获得精确回填授权。
5. `REJECTED`、字段缺失、时间无效、别名不匹配或结果来源无法确认时，EV-16 必须保持 `ABSENT`。
6. 不得把 Codex、自动化、CI、模型、机器人或 Jackie 的判断登记为 `REVIEWER-01` 的结果。

## 6. 明确非授权项

本交接包不授权：

- 阶段 B Provider 控制面核验；
- 阶段 C 私有目标核验或连接；
- 读取、验证、复制或回传凭据及余额；
- 运行真实 `preflight`、`arm` 或 `run`；
- 调用 Provider、产生费用或签发 EV-15 执行标识；
- 修改目标、部署、发布、上传或启用公开访问；
- 修改 EV-16 或其他 Evidence ID 的状态；
- 将整体结论提升为任何形式的 `GO`。

## 7. 编制完成记录

```text
Governance baseline exact: YES
Redacted EV-16 handoff prepared: YES
AUTHORIZED_OPERATOR appointment recorded: YES
EMERGENCY_STOP_OWNER appointment recorded: YES
INDEPENDENT_REVIEWER appointment recorded: YES
Human independent review performed: NO
Reviewer result: NOT_REVIEWED
EV-16 evidence status changed: NO
Other evidence status changed: NO
Stage B/C verification authorized: NO
Provider console accessed: NO
Private target connected: NO
Credentials or balances read or validated: NO
Real preflight executed: NO
arm/run executed: NO
Provider called or paid: NO
Deployment or public access: NO
Decision: NO-GO
```
