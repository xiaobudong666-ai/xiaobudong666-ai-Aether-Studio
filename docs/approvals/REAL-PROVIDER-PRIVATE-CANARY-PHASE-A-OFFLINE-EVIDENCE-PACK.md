# 真实 Provider 私有金丝雀——阶段 A 离线去敏补证包

状态：`PREPARED / EVIDENCE_INSUFFICIENT / INDEPENDENT_REVIEW_PENDING / NO-GO`

补证包 ID：`PACK-EV14-EV16-20260906-A01`

文档基线：`main@6baae165d069d64f1c9489c40b411b83f27cbbb7`

执行代码基线：`main@0d7275836abfef26db7180076b23529b4f974f26`

编制时间：`2026-09-06T17:02:56Z`

关联指引：`docs/approvals/REAL-PROVIDER-PRIVATE-CANARY-REDACTED-EVIDENCE-REMEDIATION-GUIDE.md`

关联登记：`docs/evidence/REAL-PROVIDER-PRIVATE-CANARY-REDACTED-EVIDENCE-REGISTER.md`

> 本补证包只完成 EV-14、EV-15、EV-16 的仓库内离线准备。它不构成外部事实核验、独立复核、只读预检或真实执行授权。

## 1. 获批范围

- 编制合成请求分类记录，不记录请求原文。
- 预留未使用的非秘密 approval ID 和 idempotency key 候选值。
- 编制执行、独立复核、紧急停机角色以及 `900` 秒墙钟和 `15` 分钟回滚窗口确认表。
- 证据不足时保持 `ABSENT`，不得以“材料已编制”替代 `PRESENT` 条件。

禁止连接私有目标或 Provider 控制台，禁止读取、验证或回传凭据及余额，禁止运行 `preflight`、`arm`、`run`，禁止调用 Provider、产生付费、部署或公开访问。

## 2. 阶段 A 结果

| Evidence ID | 记录 | 准备状态 | 证据状态 | 原因码 |
|---|---|---|---|---|
| EV-14 | `docs/evidence/REAL-PROVIDER-PRIVATE-CANARY-EV14-SYNTHETIC-REQUEST-CLASSIFICATION.md` | 分类门禁已编制；请求原文未保留、未复核 | `ABSENT` | `SYNTHETIC_REQUEST_NOT_PREPARED` |
| EV-15 | `docs/evidence/REAL-PROVIDER-PRIVATE-CANARY-EV15-IDENTIFIER-RESERVATION.md` | 非秘密候选标识已预留；未激活、未独立复核 | `ABSENT` | `EXECUTION_IDENTIFIER_NOT_ISSUED` |
| EV-16 | `docs/evidence/REAL-PROVIDER-PRIVATE-CANARY-EV16-STOP-ROLE-WINDOW-CONFIRMATION.md` | 时间边界已锁定；责任角色未指定 | `ABSENT` | `STOP_ROLE_NOT_ASSIGNED` |

阶段 A 没有改变证据汇总：`PRESENT 5 / ABSENT 3 / INVALID 0 / NOT_CHECKED 9`。

## 3. 后续提升条件

### EV-14

只有已获授权真人在 owner 控制的仓库外受限位置准备精确请求原文，按分类记录逐项核验，并由独立复核人接受去敏结论后，才可申请把 EV-14 改为 `PRESENT`。

### EV-15

只有 owner 对一次未来执行给出独立的精确授权，获授权真人确认两个候选标识仍唯一、未使用且与该次执行一一绑定，并经独立复核接受后，才可将候选标识签发为执行标识并申请把 EV-15 改为 `PRESENT`。

### EV-16

只有 owner 明确指定 `AUTHORIZED_OPERATOR`、`INDEPENDENT_REVIEWER` 和 `EMERGENCY_STOP_OWNER`，三方确认 `900` 秒墙钟、`15` 分钟回滚窗口、禁止不明确结果自动重试，并经独立复核接受后，才可申请把 EV-16 改为 `PRESENT`。

## 4. 完成记录

```text
Package prepared: YES
Evidence status elevated: NO
Target connected: NO
Provider console accessed: NO
Credentials or balances read: NO
preflight executed: NO
arm/run executed: NO
Provider called: NO
Paid use: NO
Deployment or public access: NO
Independent review: NOT_REVIEWED
Decision: NO-GO
```
