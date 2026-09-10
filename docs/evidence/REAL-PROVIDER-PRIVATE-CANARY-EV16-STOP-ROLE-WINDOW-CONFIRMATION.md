# EV-16——停机角色与窗口确认表

状态：`WINDOWS_LOCKED / ROLES_UNASSIGNED / NOT_REVIEWED / ABSENT`

记录 ID：`REF-EV16-PHASE-A-ROLE-WINDOW`

文档基线：`main@6baae165d069d64f1c9489c40b411b83f27cbbb7`

编制时间：`2026-09-06T17:02:56Z`

> 本表只锁定未来一次受治理金丝雀的责任槽位和时间边界。它不指定真实人员，也不授权连接目标或执行任何目标侧命令。

## 1. 责任角色

| 责任 | 角色槽位 | 当前状态 |
|---|---|---|
| 执行人 | `AUTHORIZED_OPERATOR` | `UNASSIGNED` |
| 独立复核人 | `INDEPENDENT_REVIEWER` | `UNASSIGNED` |
| 紧急停机责任人 | `EMERGENCY_STOP_OWNER` | `UNASSIGNED` |

禁止以 owner 身份、自动化进程或文档编制者身份自动填充上述槽位。真实人员只能由 owner 另行明确指定，并在仓库外受限记录中完成身份绑定。

## 2. 确定性时间边界

| 控制项 | 锁定值 | 当前状态 |
|---|---|---|
| 墙钟上限 | `900` 秒 | `LOCKED` |
| 回滚窗口 | `15` 分钟 | `LOCKED` |
| 并发 | `1` | `LOCKED` |
| 请求数 | `1` | `LOCKED` |
| 输出数 | `1` | `LOCKED` |
| 不明确结果自动重试 | 禁止 | `LOCKED` |
| 超时或结果不明确 | 进入 `UNKNOWN` 并停止 | `LOCKED` |
| 后续 `arm/run` | 必须另行明确授权 | `LOCKED` |

## 3. 证据判断

```text
Authorized operator assigned: NO
Independent reviewer assigned: NO
Emergency stop owner assigned: NO
900-second wall clock locked: YES
15-minute rollback window locked: YES
Ambiguous-result automatic retry prohibited: YES
Independent reviewer result: NOT_REVIEWED
Evidence status: ABSENT
Reason code: STOP_ROLE_NOT_ASSIGNED
```

时间边界已锁定，但三项责任角色均未指定，因此不得把 EV-16 标记为 `PRESENT`。

## 4. 安全边界

本表不包含任何人员身份、联系方式、目标地址、账户、凭据、余额或秘密路径。未连接目标或 Provider 控制台，未运行 `preflight`、`arm` 或 `run`，未调用 Provider，未产生付费，未部署或公开访问。
