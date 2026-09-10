# 真实 Provider 私有金丝雀——去敏外部证据登记

状态：`STAGED_COLLECTION_RECORDED / INDEPENDENT_REVIEW_PENDING / NO-GO`

证据采集基线：`main@7087d99298b27c3b133467caa101aa0a19d44e88`

执行代码基线：`main@0d7275836abfef26db7180076b23529b4f974f26`

本次文档对齐基线：`main@845ab5d56757b20396099f6d6dea03ef11d833fa`

阶段 A 补证包编制基线：`main@6baae165d069d64f1c9489c40b411b83f27cbbb7`

登记时间：`2026-09-03T05:43:30Z`

基线对齐时间：`2026-09-04T05:50:13Z`

阶段 A 编制时间：`2026-09-06T17:02:56Z`

## EV-01 至 EV-17 登记

| Evidence ID | Status | Reason code | Recorded at UTC | Non-secret reference |
|---|---|---|---|---|
| EV-01 | `PRESENT` | `EXECUTION_BASELINE_EXACT_AND_TREE_STABLE` | `2026-09-04T05:50:13Z` | `REF-EXECUTION-BASELINE-0D727583` |
| EV-02 | `NOT_CHECKED` | `HUMAN_TARGET_VERIFICATION_REQUIRED` | `2026-09-03T05:43:30Z` | `TARGET-PRIVATE-01` |
| EV-03 | `NOT_CHECKED` | `HUMAN_PROVIDER_CONTROL_PLANE_REQUIRED` | `2026-09-03T05:43:30Z` | `NONE` |
| EV-04 | `NOT_CHECKED` | `HUMAN_PROVIDER_CONTROL_PLANE_REQUIRED` | `2026-09-03T05:43:30Z` | `NONE` |
| EV-05 | `PRESENT` | `PUBLIC_TERMS_SCOPE_CONFIRMED` | `2026-09-03T05:43:30Z` | `https://www.pexels.com/terms-of-service/` |
| EV-06 | `NOT_CHECKED` | `TARGET_VERSION_HUMAN_VERIFICATION_REQUIRED` | `2026-09-03T05:43:30Z` | `https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support` |
| EV-07 | `NOT_CHECKED` | `HUMAN_TARGET_VERIFICATION_REQUIRED` | `2026-09-03T05:43:30Z` | `TARGET-PRIVATE-01` |
| EV-08 | `PRESENT` | `REPOSITORY_CONFIGURATION_LOCK_CONFIRMED` | `2026-09-03T05:43:30Z` | `REF-REPO-CANARY-CONTRACT` |
| EV-09 | `NOT_CHECKED` | `TARGET_CONFIGURATION_HUMAN_VERIFICATION_REQUIRED` | `2026-09-03T05:43:30Z` | `TARGET-PRIVATE-01` |
| EV-10 | `NOT_CHECKED` | `TARGET_CONFIGURATION_HUMAN_VERIFICATION_REQUIRED` | `2026-09-03T05:43:30Z` | `TARGET-PRIVATE-01` |
| EV-11 | `NOT_CHECKED` | `TARGET_GOVERNANCE_BINDING_HUMAN_REQUIRED` | `2026-09-03T05:43:30Z` | `TARGET-PRIVATE-01` |
| EV-12 | `PRESENT` | `REPOSITORY_SINGLE_TASK_BUDGET_CONFIRMED` | `2026-09-03T05:43:30Z` | `REF-REPO-CANARY-CONTRACT` |
| EV-13 | `PRESENT` | `REPOSITORY_ARTIFACT_RIGHTS_BOUNDARY_CONFIRMED` | `2026-09-03T05:43:30Z` | `REF-REPO-CANARY-CONTRACT` |
| EV-14 | `ABSENT` | `SYNTHETIC_REQUEST_NOT_PREPARED` | `2026-09-06T17:02:56Z` | `REF-EV14-PHASE-A-CLASSIFICATION` |
| EV-15 | `ABSENT` | `EXECUTION_IDENTIFIER_NOT_ISSUED` | `2026-09-06T17:02:56Z` | `REF-EV15-PHASE-A-RESERVATION` |
| EV-16 | `ABSENT` | `STOP_ROLE_NOT_ASSIGNED` | `2026-09-06T17:02:56Z` | `REF-EV16-PHASE-A-ROLE-WINDOW` |
| EV-17 | `NOT_CHECKED` | `HUMAN_RETENTION_LOCATION_VERIFICATION_REQUIRED` | `2026-09-03T05:43:30Z` | `NONE` |

## 非秘密引用

| Reference | Non-secret subject |
|---|---|
| `REF-EXECUTION-BASELINE-0D727583` | 精确执行代码基线及其到文档对齐基线之间实现树无差异的只读 Git 记录 |
| `REF-REPO-CANARY-CONTRACT` | 唯一候选组合、单任务预算、`/tasks/` 产物边界、rights-blocked 与 `adopted=false` 的基线内治理合同 |
| `TARGET-PRIVATE-01` | owner 已批准使用的非秘密目标别名；不包含实际地址或访问参数 |
| `REF-EV14-PHASE-A-CLASSIFICATION` | 合成请求分类门禁已编制；精确请求来源与独立复核仍缺失 |
| `REF-EV15-PHASE-A-RESERVATION` | 非秘密候选标识已预留；尚未签发、激活或独立复核 |
| `REF-EV16-PHASE-A-ROLE-WINDOW` | `900` 秒墙钟与 `15` 分钟回滚窗口已锁定；责任角色仍未指定 |

## 汇总

```text
Execution code baseline: main@0d7275836abfef26db7180076b23529b4f974f26
Evidence collection baseline: main@7087d99298b27c3b133467caa101aa0a19d44e88
Governance document baseline: main@845ab5d56757b20396099f6d6dea03ef11d833fa
Stage A package baseline: main@6baae165d069d64f1c9489c40b411b83f27cbbb7
Evidence present: 5/17
Evidence absent: 3/17
Evidence invalid: 0/17
Evidence not checked: 9/17
Independent review: NOT_REVIEWED
Read-only preflight: NOT_AUTHORIZED / NOT_EXECUTED
Execution approval: ABSENT
Decision: NO-GO
```

本登记不包含 Provider 控制台、私有目标、凭据值、真实 IP、账户、秘密路径或原始证据；未运行 `preflight`，未执行 `arm/run`，未调用 Provider，未产生付费，未部署或公开访问。
