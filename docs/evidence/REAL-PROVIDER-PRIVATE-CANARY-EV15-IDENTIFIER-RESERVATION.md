# EV-15——非秘密执行标识预留记录

状态：`CANDIDATES_RESERVED / NOT_ISSUED / NOT_ARMED / NOT_REVIEWED / ABSENT`

记录 ID：`REF-EV15-PHASE-A-RESERVATION`

文档基线：`main@6baae165d069d64f1c9489c40b411b83f27cbbb7`

预留时间：`2026-09-06T17:02:56Z`

> 下列值是公开、非秘密、未激活的候选标识。预留不代表执行审批，不允许用于 `preflight`、`arm` 或 `run`。

## 1. 候选标识

| 项目 | 非秘密候选值 |
|---|---|
| Approval ID | `AETHER-PC-A-APPROVAL-c1caf41f-c20e-4359-a9de-bc15cc178bb1` |
| Idempotency key | `aether-pc-a-idem-912b280a-8036-492a-8dbc-807ff55c09c0` |
| 执行代码绑定 | `main@0d7275836abfef26db7180076b23529b4f974f26` |
| 目标别名绑定 | `TARGET-PRIVATE-01` |
| 最大未来执行次数 | `1` |
| 当前激活状态 | `NOT_ISSUED / NOT_ARMED` |

## 2. 离线校验

| 检查 | 结果 |
|---|---|
| UUID 结构 | `PASS` |
| 预留前仓库精确匹配次数 | `0` |
| 预留动作是否调用目标或 Provider | `NO` |
| 是否已获真实执行授权 | `NO` |
| 是否已经用于 `preflight`、`arm` 或 `run` | `NO` |
| 是否完成独立复核 | `NO` |

## 3. 证据判断

```text
Reservation state: RESERVED
Execution identifier issuance: NOT_ISSUED
Usage state: UNUSED_BY_THIS_WORKFLOW
External usage verification: NOT_CHECKED
Independent reviewer result: NOT_REVIEWED
Evidence status: ABSENT
Reason code: EXECUTION_IDENTIFIER_NOT_ISSUED
```

候选值只有在另行取得一次真实执行授权、再次确认唯一且未使用并完成独立复核后，才可签发。当前不得把 EV-15 标记为 `PRESENT`。

## 4. 安全边界

本记录不含账户、凭据、余额、目标地址或秘密路径。未连接目标或 Provider 控制台，未运行 `preflight`、`arm` 或 `run`，未调用 Provider，未产生付费，未部署或公开访问。
