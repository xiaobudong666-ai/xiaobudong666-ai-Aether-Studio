# 真实 Provider 私有金丝雀——执行前验收表

状态：`TEMPLATE_ONLY / NOT_ACCEPTED / EXECUTION_NOT_AUTHORIZED`

编制基线：`main@e43c71166a6e525cad23c47dfd5f30a980d04625`

执行代码基线：`main@0d7275836abfef26db7180076b23529b4f974f26`

本次文档对齐基线：`main@845ab5d56757b20396099f6d6dea03ef11d833fa`

> 本表用于未来汇总已经独立采集和复核的去敏证据。当前所有运行相关项均为未验收；填写或合并本表不构成凭据、预检、Provider 调用、付费、`arm/run`、部署或公开访问授权。

## 1. 固定候选配置

| 项目 | 唯一允许值 | 当前结论 |
|---|---|---|
| 精确代码基线 | `main@0d7275836abfef26db7180076b23529b4f974f26` | `LOCKED` |
| 目标别名 | `TARGET-PRIVATE-01` | `LOCKED`；不记录实际地址 |
| LLM | DeepSeek 官方 `deepseek-v4-flash` | `LOCKED` |
| 素材 | Pexels；仅非人物、非品牌、非私人场所 | `LOCKED` |
| 语音 | Edge `zh-CN-XiaoxiaoNeural` | `LOCKED` |
| Profile | `private-one-task-v1` | `LOCKED` |
| 并发/请求/输出 | `1 / 1 / 1` | `LOCKED` |
| 生成时长 | 不超过 `10` 秒 | `LOCKED` |
| Provider 总费用硬上限 | `CNY 5.00` | `NOT_VERIFIED` |
| 墙钟/回滚 | 不超过 `900` 秒 / `15` 分钟 | `LOCKED` |

## 2. 门禁验收矩阵

| Gate | 必须满足 | 证据 ID | 当前状态 | Reviewer |
|---|---|---|---|---|
| G-01 | main SHA 精确匹配且工作树干净 | EV-01 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-02 | owner 控制的私有目标且无公网 Sidecar | EV-02 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-03 | DeepSeek 硬费用上限与账户风险可接受 | EV-03, EV-04 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-04 | Pexels 使用条件满足首次合成主题 | EV-05 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-05 | Edge TTS 路径与语音在目标版本可用 | EV-06 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-06 | 目标本地文件结构、属主、权限合规 | EV-07 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-07 | Provider/素材/语音唯一组合且无代理/自定义端点 | EV-08, EV-09 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-08 | 日志、隐藏配置、自动上传和发布边界合规 | EV-10 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-09 | tenant/config/policy/project 绑定一致 | EV-11 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-10 | 单任务预算与 `/tasks/` 产物权利边界有效 | EV-12, EV-13 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-11 | 合成、非身份化、无输入资产请求通过校验 | EV-14 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-12 | approval/idempotency 唯一且未使用 | EV-15 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-13 | 停机责任、900 秒墙钟、15 分钟回滚明确 | EV-16 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-14 | 去敏证据位置和保留边界合规 | EV-17 | `NOT_CHECKED` | `NOT_REVIEWED` |
| G-15 | 只读预检获得独立授权并通过 | 独立预检记录 | `NOT_AUTHORIZED` | `NOT_REVIEWED` |
| G-16 | 一次真实执行获得独立精确授权 | EV-18 | `ABSENT` | `NOT_REVIEWED` |

## 3. 强制否决规则

以下任一项成立即为 `NO-GO`，不得由风险接受备注覆盖：

- G-01 至 G-16 任一不是 `ACCEPTED`；
- SHA、目标别名、Provider、模型、素材源、语音或预算与固定值不一致；
- 需要披露秘密才能解释结果；
- 费用硬上限只依赖 Aether 内部配额而未在 Provider 或等效外部控制面落实；
- 请求包含真人、面部、声音克隆、客户、第三方身份、URL、邮箱、上传资产或私人信息；
- 准备自动重试不明确提交、扩大为第二任务、自动采纳、时间线写入、渲染或发布；
- 预检与真实执行授权被合并为模糊的“继续”“开始”或“按方案执行”。

## 4. 验收签署模板

```text
Acceptance ID: <NON_SECRET_ID>
Execution code baseline: main@0d7275836abfef26db7180076b23529b4f974f26
Governance document baseline: main@845ab5d56757b20396099f6d6dea03ef11d833fa
Target alias: TARGET-PRIVATE-01
Evidence checklist version: <COMMIT_SHA>
Preflight result: ACCEPTED | REJECTED | NOT_AUTHORIZED
Gates accepted: 0/16
Independent reviewer role: <ROLE_OR_NOT_ASSIGNED>
Reviewed at UTC: <TIMESTAMP_OR_NOT_REVIEWED>
Execution approval: ABSENT | PRESENT
Final decision: NO-GO | GO_FOR_ONE_AUTHORIZED_EXECUTION
```

只有 `Gates accepted: 16/16`、`Preflight result: ACCEPTED`、`Execution approval: PRESENT` 三项同时成立，最终结论才允许写为 `GO_FOR_ONE_AUTHORIZED_EXECUTION`。该结论仍只允许审批中精确描述的一次执行，不得扩展。

## 5. 当前验收结论

```text
Documentation package: PREPARED
External evidence collected: 0/18
Read-only preflight authorized: NO
Read-only preflight executed: NO
Execution approval: ABSENT
Gates accepted: 0/16
Final decision: NO-GO
```

当前仅完成模板编制，没有发生任何目标访问、凭据操作、Provider 调用、付费、`arm/run`、部署或公开访问。
