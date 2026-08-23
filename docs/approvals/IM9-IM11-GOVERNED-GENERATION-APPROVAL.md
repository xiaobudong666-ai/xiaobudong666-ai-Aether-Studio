# IM9–IM11《受治理生成任务与结果入库》编码审批包

> 文档状态：Draft / 编码前审批包
> 基线：`main`（IM6–IM8 已合并）
> 本文档是产品/工程边界，不代表代码已实现、测试已通过、真实模型/插件已接入、付费调用、部署或商用批准。

## 1. 目标与范围

IM9–IM11 负责把“生成请求”安全地编排为可追踪任务，并把完成结果纳入 Aether Studio 素材与候选成片体系。主链为：`CreateRequest → Preflight → Queued → Running → Succeeded/Failed/Cancelled → ResultReview → AssetVersion`。

### 允许范围
- 前端生成任务表单、预检、状态展示、取消/重试入口。
- 任务状态机、幂等键、请求快照、结果元数据与候选结果展示。
- 使用 fake/local adapter 做确定性测试。
- 结果入库前执行权利快照、来源和租户/项目一致性校验。

### 禁止范围
- 真实第三方模型、插件、API key、付费调用。
- 新增后端 API、数据库迁移、Worker、队列基础设施。
- 自动采纳生成结果为最终成片。
- 自动写入未经治理的素材、时间线或公开访问地址。
- 部署与生产商用。

## 2. 页面与交互

### P9-1 生成任务面板
字段：项目、提示词、参考素材、目标比例、时长、输出数量、模型能力占位、权利声明。
按钮：预检、提交生成、取消、重试、查看结果。
规则：必填字段缺失不可提交；权利快照缺失直接 `BLOCKED`；重复点击只产生一个 client request id。

### P10-1 任务中心
展示：任务 ID、项目、创建者、状态、耗时、进度、错误码、创建时间、更新时间。
状态：`DRAFT / PREFLIGHT / BLOCKED / QUEUED / RUNNING / SUCCEEDED / FAILED / CANCELLED / EXPIRED`。

### P11-1 结果审阅
展示候选结果缩略图/视频、来源、版本、权利状态、生成参数摘要。
按钮：预览、重新生成、进入剪辑、放弃。
“进入剪辑”只创建引用，不自动写入最终时间线；权利不满足时禁止继续。

## 3. 权限
- Owner/Admin：创建、取消、重试、审阅、采纳候选结果。
- Editor：创建、审阅、进入剪辑；不得修改团队权限。
- Viewer：只读任务与结果。
- 所有操作必须校验 tenant/project scope。

## 4. API/适配边界
前端契约：`POST /api/generation/preflight`、`POST /api/generation/tasks`、`GET /api/generation/tasks/:id`、`POST /api/generation/tasks/:id/cancel`、`POST /api/generation/tasks/:id/retry`、`GET /api/generation/tasks/:id/results`。

在本阶段这些仅作为契约，不得新增真实后端实现。开发实现应通过既有 mock/fake adapter 驱动 UI 与状态机；未来后端接入必须另行审批。

## 5. 数据结构
```ts
GenerationRequest { id, clientRequestId, tenantId, projectId, prompt, inputAssetIds, aspectRatio, durationMs, outputCount, rightsSnapshotIds, createdBy, createdAt }
GenerationTask { id, requestId, status, progress, attempt, errorCode, errorMessage, createdAt, updatedAt }
GenerationResult { id, taskId, assetVersionId, sourceUri, checksum, mimeType, durationMs, width, height, rightsSnapshotId, provenance, createdAt }
```

## 6. 状态机与异常
- `DRAFT → PREFLIGHT`：字段完整且用户主动预检。
- `PREFLIGHT → BLOCKED`：权利、范围、项目或配额条件不满足。
- `PREFLIGHT → QUEUED`：预检通过。
- `QUEUED → RUNNING → SUCCEEDED`：fake adapter 正常返回。
- `RUNNING → FAILED`：可重试错误；重试必须产生新 attempt 并保留历史。
- `RUNNING → CANCELLED`：用户主动取消。
- 迟到响应不得覆盖已取消、已切换项目或更高 attempt 的状态。
- 409/版本冲突：停止自动推进，要求重新预检。

## 7. 模块联动
M01/M02 项目上下文 → M03/M04 素材与脚本 → IM9 生成请求 → IM10 任务状态 → IM11 结果候选 → M05–M08 编辑/短视频工作台 → M10 成片中心。
任何环节不得绕过权利治理直接写最终时间线。

## 8. 验收用例
1. 缺提示词不可预检。
2. 缺权利快照进入 BLOCKED。
3. BLOCKED 状态不产生时间线写入。
4. 重复提交只产生一个 clientRequestId。
5. 项目切换后迟到响应不污染新项目。
6. cancel 后迟到成功结果不恢复任务。
7. 失败重试保留 attempt 历史。
8. 409 后必须重新预检。
9. fake adapter 成功结果生成唯一 checksum。
10. 结果无 rightsSnapshotId 不得进入审阅通过态。
11. Viewer 无提交按钮。
12. 跨 tenant/project 请求被拒绝。
13. 结果进入剪辑仅创建引用，不自动采纳。
14. 重复结果不会生成重复 AssetVersion。
15. 输出数量达到上限时阻止提交。
16. 不支持的比例被预检拒绝。
17. 取消按钮仅在可取消状态显示。
18. 已成功任务不可再次 cancel。
19. 失败任务无可重试错误时隐藏 retry。
20. 任务列表分页/刷新不改变状态机事实。
21. 错误信息不泄露凭据。
22. provenance 缺失的结果不可采纳。
23. checksum 冲突进入 BLOCKED。
24. 结果审阅与任务项目不一致时禁止继续。
25. 用户关闭页面后重新进入可恢复任务状态。
26. 同一任务重复 refresh 不重复创建结果。
27. 生成参数快照与结果可追溯。
28. 所有关键操作记录 actor 与时间。

## 9. 编码门禁
只有明确批准本审批包后，才能进入功能编码；正式评审、合并、真实模型/插件接入、付费调用、部署均另行审批。
