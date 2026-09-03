# 真实 Provider 私有金丝雀——只读预检操作手册

状态：`RUNBOOK_ONLY / PREFLIGHT_NOT_AUTHORIZED / NOT_EXECUTED`

编制基线：`main@e43c71166a6e525cad23c47dfd5f30a980d04625`

> 本手册描述未来在取得独立授权后如何执行去敏、只读预检。当前不得运行本文命令，不得连接 `TARGET-PRIVATE-01`，不得读取或验证真实凭据，也不得调用 Provider。本文不包含 `arm`、`run`、部署或公开访问步骤。

## 1. 适用条件

只有同时满足以下条件，获授权执行人才可使用本手册：

1. 外部证据清单 `EV-01` 至 `EV-17` 均为 `PRESENT`；
2. owner 已对精确 main SHA 与目标别名单独批准“只读预检”；
3. 执行窗口、执行人、复核人和紧急停止责任人已登记；
4. 执行环境已确认不会把 shell history、环境变量、配置内容或路径上传到日志/工单/聊天；
5. 当前目标仍保持停机，`AETHER_CANARY_REAL_EXECUTION_APPROVED` 不得设为 `true`。

任一条件不满足，立即停止，结论为 `NO-GO`。

## 2. 角色分离

| 角色 | 允许动作 | 禁止动作 |
|---|---|---|
| Owner | 批准精确 SHA、目标别名和预检窗口 | 以口头“继续”替代精确执行授权 |
| Authorized operator | 在目标本地设置非回显环境并运行一次只读预检 | 输出变量、打开配置、试调用 Provider、执行 `arm/run` |
| Independent reviewer | 复核稳定状态码和去敏输出 | 索要秘密值、路径或原始截图 |

## 3. 执行前人工核验

以下检查只记录结论，不记录敏感值：

- 当前工作树是批准 SHA 的干净检出；
- 目标别名与审批一致，且为 owner 控制的私有目标；
- 四类目标本地文件均在仓库外、非符号链接、属主正确、权限不宽于 `0600`；
- DeepSeek 费用硬上限、Pexels 条款、Edge TTS 版本证据均为 `PRESENT`；
- tenant/config version/policy/project 绑定已由外部治理记录确认；
- kill switch 保持 `DISABLED`，operator mode 保持 `disabled`；
- 没有任何容器需要因预检而启动。

## 4. 未来获授权后的命令模板

以下是不可直接复制执行的结构模板。尖括号表示只在目标本地安全输入的值；不得把替换后的命令保存到仓库、聊天、CI、工单或终端截图。

```bash
# 仅在 owner 单独批准只读预检后，由目标本地获授权执行人使用。
# 使用无历史记录的受控 shell；不得开启 xtrace（set -x）。

export MONEYPRINTER_CONFIG_FILE='<TARGET_LOCAL_SECRET_PATH>'
export AETHER_CANARY_ENV_FILE='<TARGET_LOCAL_ENV_PATH>'
export AETHER_CANARY_LLM_PROVIDER='deepseek'
export AETHER_CANARY_MODEL='deepseek-v4-flash'
export AETHER_CANARY_MATERIAL_SOURCE='pexels'
export AETHER_CANARY_VOICE_PATH='edge'
export AETHER_GENERATION_TENANT_ID='<NON_SECRET_TENANT_ID>'
export AETHER_GENERATION_CONFIG_VERSION_ID='<NON_SECRET_CONFIG_VERSION_ID>'
export AETHER_GENERATION_POLICY_HASH='<64_HEX_POLICY_HASH>'
export AETHER_CANARY_PROVIDER_BUDGET_EVIDENCE='PRESENT'
export AETHER_CANARY_MATERIAL_LICENSE_EVIDENCE='PRESENT'
export AETHER_CANARY_CONCURRENT_LIMIT='1'
export AETHER_CANARY_REQUEST_LIMIT='1'
export AETHER_CANARY_GENERATED_SECONDS_LIMIT='10'
export AETHER_CANARY_OUTPUT_LIMIT='1'
export AETHER_CANARY_ARTIFACT_PATH_PREFIX='/tasks/'

./infra/docker/provider-canary.sh preflight \
  --approved-sha e43c71166a6e525cad23c47dfd5f30a980d04625
```

必须保持以下变量不存在或为 `false`：

```bash
unset AETHER_CANARY_REAL_EXECUTION_APPROVED
unset AETHER_CANARY_OWNER_CONFIRMED
```

不得设置执行所需的 approval ID、owner cookie、API base URL、request file、idempotency key 或 state file；只读预检不需要这些变量。

## 5. 预期去敏输出

允许的成功输出仅应表达以下非秘密事实：

```json
{
  "status": "PREFLIGHTED",
  "credentialState": "PRESENT",
  "networkIsolation": "ENFORCED",
  "canaryProfile": "private-one-task-v1"
}
```

`credentialState=PRESENT` 仅表示文件结构和固定配置约束通过，不证明凭据真实有效、账户余额充足、Provider 可访问或生成成功。

失败只记录稳定 `reasonCode`。如果输出出现路径、URL、账户、余额、配置字段、提示、请求体、响应体、Key、Cookie、Token 或 Authorization，立即停止并将结果判为 `EVIDENCE_CONTAMINATED / NO-GO`；不得提交该输出。

## 6. 预检后动作

无论预检成功或失败，均必须：

1. 保持 kill switch 为 `DISABLED`；
2. 保持 operator mode 为 `disabled`；
3. 确认未启动 Sidecar/Worker override 栈；
4. 清除当前受控 shell 中为预检设置的环境变量；
5. 只把允许字段填入执行前验收表；
6. 由独立复核人给出 `ACCEPTED` 或 `REJECTED`；
7. 停止，不得自动转入 `arm` 或 `run`。

## 7. 停止条件

出现以下任一情况立即停止：SHA 不符、工作树不净、目标身份不明、文件权限过宽、文件位于仓库内、存在代理或自定义端点、Provider 组合不符、费用/许可证证据缺失、policy 不匹配、输出可能泄密、命令返回不明确、容器被意外启动或有人要求跳过独立执行授权。

## 8. 当前记录

```text
Runbook prepared: YES
Target connected: NO
Credentials accessed: NO
Preflight executed: NO
Provider called: NO
Paid use: NO
arm/run executed: NO
Decision: NO-GO UNTIL SEPARATELY AUTHORIZED
```
