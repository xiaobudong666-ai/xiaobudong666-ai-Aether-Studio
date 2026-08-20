# IM-6～IM-8 一键短视频制作工作台——编码审批包

> Status: `DRAFT_FOR_OWNER_APPROVAL`
> Prepared: 2026-08-21 (UTC+8)
> Authoritative baseline: `main@46c7804a50bf439b7046e19fd04764bb5b63ca16`
> Depends on: accepted IM-1/IM-2 foundation and accepted IM-3/IM-5 governed workbench operations
> Decision owner: Aether Studio one-person OPC owner
> Document completion is not code implementation, test passage, deployment, provider/plugin/model access or commercial approval.

## 1. 本次请求批准的决策

本审批包请求批准一个严格受限的前端工作台增量，将仓库已经存在并已验收的项目、素材、时间线、权利检查、渲染任务、候选成片和母版查看能力，组织为可理解、可中断、可恢复的“一键短视频制作”流程。

本批次中的“一键”具有明确边界：

1. 用户在提交前仍须选择素材、确认顺序、确认时间线覆盖策略并查看权利预检。
2. 用户完成一次明确确认后，客户端才按固定顺序执行现有 API。
3. 客户端最多提交一次渲染请求；双击、重复点击和迟到响应不得产生第二个任务。
4. 成功渲染只生成现有 Candidate；不得自动采纳为 MasterRevision。
5. 本批次不生成 AI 图片、AI 视频、数字人、换脸、换人、变装、换景、配音或提示词优化结果。
6. 本批次不接入或调用 MoneyPrinterTurbo 的真实上游提供商、真实模型、短视频插件或任何付费服务。
7. 本文档获批仅授权后续按第 11.1 节实施；当前文档本身不代表代码已经实现。

## 2. 基线事实

### 2.1 已存在并可复用的前端能力

- 登录、退出、当前用户和 owner/editor/viewer 角色显示。
- 项目列表、项目创建、项目选择、项目详情加载与乐观并发修订。
- 单文件素材上传、媒体探测结果、AssetVersion 与 SHA-256 展示。
- 将素材加入 Canonical Timeline 1.1、选择片段、调整属性、预览和保存。
- 提交 FFmpeg/video-use 渲染任务。
- CanonicalTaskStatus、SSE、手动刷新、Candidate、Adoption 和 MasterRevision 展示。
- OpenCut 兼容快照与 OpenReel 项目文件导出。
- 项目切换时的异步迟到响应隔离。

### 2.2 已存在并可复用的 API

| API | 本批用途 | 写入性质 |
|---|---|---|
| `GET /auth/me` | 读取角色、租户和配额 | 只读 |
| `GET /projects` | 加载可选项目 | 只读 |
| `POST /projects` | 新建快速制作项目 | 写入，owner/editor |
| `GET /projects/{project_id}` | 加载项目与当前修订 | 只读 |
| `PUT /projects/{project_id}` | 一次性保存自动排布后的时间线 | 写入，owner/editor，带 expectedRevision |
| `POST /projects/{project_id}/media` | 按顺序上传本次选定素材 | 写入，owner/editor |
| `GET /projects/{project_id}/asset-versions` | 对齐 Material 与 AssetVersion | 只读 |
| `GET /projects/{project_id}/asset-versions/{asset_version_id}/rights-check` | 提交前权利预检 | 只读 |
| `POST /projects/{project_id}/render` | 只提交一次渲染任务 | 写入，owner/editor |
| `GET /render-tasks?project_id=...` | 刷新规范任务状态 | 只读 |
| `GET /projects/{project_id}/candidates` | 渲染成功后交接候选成片 | 只读 |
| `GET /projects/{project_id}/masters` | 查看已有不可变母版 | 只读 |
| `GET /events` | 接收规范任务进度 | 只读流 |

不得新增 API 路由，不得修改后端模型、数据库迁移、Worker、队列、渲染器、认证或基础设施。

### 2.3 当前仍未实现的能力

- M03 数字人。
- M05 换脸换人。
- M06 变装换景。
- 真实 AI 图片/视频模型和质量门禁。
- 自动脚本拆镜、故事圣经和镜头生产权威。
- 自动配音、音乐生成和提示词优化。
- 发布、撤回、平台分发和商业运营。
- 自动采纳候选成片为母版。

以上能力不得通过文案、空按钮、模拟成功状态或伪造数据出现在本批次中。

## 3. IM-6——快速制作入口与素材编排

### 3.1 页面落位

在现有 React 工作台内增加 `QuickCreatePanel`，作为工作台顶部的“快速制作”入口，不新增路由、不建立第二套工作台、不替换现有素材库和时间线。

入口显示规则：

- owner/editor：显示“快速制作短视频”按钮。
- viewer：显示只读说明，不显示可触发写操作的主按钮。
- 未选择项目：允许选择“新建项目”。
- 已选择项目：允许选择“使用当前项目”，并显示当前修订号。
- 当前存在未保存或正在提交操作时：入口禁用并说明原因。

### 3.2 字段定义

| 字段 | 类型/约束 | 默认值 | 说明 |
|---|---|---|---|
| `projectMode` | `NEW_PROJECT / CURRENT_PROJECT` | 有当前项目时为 `CURRENT_PROJECT` | 决定创建新项目还是使用当前项目 |
| `projectName` | 字符串，1～120 字符 | 空 | 仅新建项目时必填；前后空白被裁剪 |
| `existingMediaIds` | 字符串数组，0～20 项 | 空 | 从当前项目选择既有素材 |
| `newFiles` | 浏览器 File 数组；与既有素材合计 1～20 项 | 空 | 按用户确认顺序逐个上传；服务端配额仍是最终边界 |
| `clipMode` | `ORIGINAL_DURATION / FIXED_DURATION` | `ORIGINAL_DURATION` | 原时长或统一时长 |
| `fixedClipSeconds` | 整数，1～30 | 3 | 仅固定时长模式启用；不得超过已知源时长 |
| `replaceExistingTimeline` | 布尔值 | false | 当前时间线非空时必须显式勾选 |
| `requireExportRights` | 只读布尔值 | true | 第一批固定开启，不提供绕过 |
| `confirmRender` | 布尔值 | false | 提交前确认会创建真实渲染任务 |

浏览器 File 对象、临时预览 URL 和本次运行状态只保存在当前页面内存中，不写入 localStorage、日志、URL 或分析事件。

### 3.3 按钮和交互

| 按钮 | 条件 | 动作 |
|---|---|---|
| “快速制作短视频” | owner/editor，当前无冲突写操作 | 打开面板 |
| “选择素材” | 面板打开 | 选择既有素材或本地文件 |
| “上移/下移” | 对应素材存在 | 调整确定性顺序 |
| “移除” | 至少保留 1 个素材 | 从本次草稿移除，不删除仓库素材 |
| “执行预检” | 字段校验通过 | 读取项目修订、素材版本和权利状态，不写入 |
| “一键生成短视频” | 预检通过且已确认 | 按第 3.4 节执行写入链路 |
| “取消” | 尚未开始写入 | 清空本次内存草稿并关闭 |
| “查看任务” | 渲染请求已返回 taskId | 聚焦现有任务面板 |
| “查看候选成片” | 任务规范状态为 SUCCEEDED | 聚焦现有 FinishedMediaPanel |

不得出现“自动发布”“自动采纳”“一键换脸”“一键变装”或任何尚未实现的按钮。

### 3.4 单次执行顺序

执行链路必须串行且可审计：

1. 再次核验 owner/editor 角色、活动项目和面板 request generation。
2. 新建模式下调用一次 `POST /projects`；当前项目模式下重新读取项目修订。
3. 按确认顺序逐个调用现有媒体上传 API，并逐项记录成功或失败。
4. 任一上传失败时停止后续自动步骤，保留已经由服务端成功创建的不可变素材版本，展示“部分完成”，不得伪造回滚。
5. 根据成功素材生成确定性 Canonical Timeline 1.1。
6. 当前时间线非空且未确认覆盖时停止，不得写入。
7. 使用最新 `expectedRevision` 调用一次 `PUT /projects/{project_id}`。
8. 对本次时间线引用的每个 AssetVersion 执行权利检查。
9. 任一权利检查非 `RIGHTS_ALLOWED` 时进入 BLOCKED，列出素材、版本和原因，不提交渲染。
10. 所有权利检查通过后，且 `confirmRender=true`，调用一次 `POST /projects/{project_id}/render`。
11. 收到 taskId 后交由现有任务/SSE/候选成片界面继续跟踪。
12. 不调用 adoption API，不创建 MasterRevision。

## 4. IM-7——确定性时间线自动排布

### 4.1 排布规则

- 所有片段进入现有第一条 video track；没有 video track 时在客户端创建一条。
- 片段顺序严格等于用户确认的素材顺序。
- 第一片段 start 为 `0/24000`。
- 后续片段 start 等于此前所有片段 duration 的 RationalTime 精确和。
- `ORIGINAL_DURATION` 使用 Material.duration；缺少时长的 image 默认 3 秒，缺少时长的 video/audio 阻塞并要求用户处理。
- `FIXED_DURATION` 使用 1～30 秒配置；当已知源时长更短时取源时长。
- `sourceIn` 固定为 `0/24000`。
- 不创建重叠片段、不自动裁掉现有轨道、不修改音量、透明度、位置或画布尺寸。
- 自动生成的 clipId 和 trackId 必须使用已有客户端 ID 策略，且在单次保存前保持稳定。
- 写入前必须通过现有 TimelineSchema/ProjectDTO 类型边界。
- 完成后仍可使用现有时间线和属性面板手动调整。

### 4.2 覆盖保护

当当前项目时间线已有任意 clip：

- 默认不覆盖。
- 明确显示现有 clip 数和项目修订号。
- 用户必须勾选 `replaceExistingTimeline`。
- 保存发生 409/并发冲突时，不自动重试、不用旧草稿覆盖新修订；刷新项目并要求用户重新预检。

## 5. IM-8——权利预检、单次渲染和结果交接

### 5.1 权利预检

每个本次使用素材必须映射到同项目 AssetVersion，并展示：

- 素材名称和 mediaId；
- assetVersionId 和 versionNo；
- SHA-256 缩略值；
- `RIGHTS_ALLOWED / MISSING / DENIED / REVOKED / UNKNOWN / NOT_YET_VALID / EXPIRED`；
- 有效期；
- 是否允许继续。

无法映射 AssetVersion、接口异常或未知状态均按阻塞处理，不得按允许处理。

### 5.2 单次提交保护

- 主按钮在进入 SUBMITTING 后立即禁用。
- 组件维护本次运行 token 和 projectId。
- 迟到响应只有在 token、projectId 和当前 request generation 同时匹配时才能更新界面。
- 网络在“请求已发送但响应未知”时不得自动重新 POST；先刷新 render tasks，再由用户明确决定。
- 当前后端 render API 没有客户端 idempotency key，本批不得宣称服务端幂等；只能实现前端单次提交保护。
- 任务提交成功后保留 taskId，并使用现有任务状态权威和 SSE。

### 5.3 结果交接

- QUEUED/RUNNING：显示任务、进度、消息和离开页面后可返回查看的说明。
- SUCCEEDED：显示已有 Candidate 和下载入口。
- FAILED/CANCELED/PARTIAL/UNKNOWN：沿用规范状态语义，不伪装成功。
- Candidate 的“采纳为母版”继续使用现有独立确认流程。
- 一键流程绝不自动调用 `POST /projects/{project_id}/candidates/{candidate_id}/adopt`。

## 6. 状态机

| 状态 | 可进入条件 | 可执行动作 | 退出条件 |
|---|---|---|---|
| `IDLE` | 面板关闭或草稿为空 | 打开 | 用户打开 |
| `EDITING` | 面板打开 | 编辑字段、排序、取消、预检 | 预检或取消 |
| `VALIDATING` | 点击预检 | 只读校验 | READY/BLOCKED/FAILED |
| `READY` | 字段、修订、素材映射和权利均通过 | 一键生成、返回编辑 | 用户确认 |
| `CREATING_PROJECT` | 新建项目模式 | 等待 | 成功或失败 |
| `UPLOADING` | 存在新文件 | 显示逐项进度 | 全部成功或部分失败 |
| `ARRANGING` | 素材可排布 | 构建内存时间线 | SAVING/FAILED |
| `SAVING` | 通过覆盖与修订检查 | 等待一次 PUT | RIGHTS_CHECKING/CONFLICT/FAILED |
| `RIGHTS_CHECKING` | 时间线保存成功 | 只读检查 | SUBMITTING/BLOCKED/FAILED |
| `SUBMITTING` | 权利全通过且确认渲染 | 禁止重复点击 | TRACKING/AMBIGUOUS/FAILED |
| `TRACKING` | taskId 已确认 | 查看任务 | 规范终态 |
| `SUCCEEDED` | 规范成功 | 查看候选成片 | 用户关闭 |
| `BLOCKED` | 权利/映射/输入阻断 | 返回编辑或治理素材 | 重新预检 |
| `PARTIAL` | 部分上传已成功 | 查看成功项，手动继续 | 用户处理 |
| `CONFLICT` | 项目修订冲突 | 刷新项目 | 重新编辑 |
| `FAILED` | 可解释失败 | 返回编辑 | 用户处理 |

状态不可跳过；刷新页面后不得凭客户端内存声称流程成功，必须重新读取服务端项目和任务状态。

## 7. 权限矩阵

| 能力 | owner | editor | viewer |
|---|---:|---:|---:|
| 查看快速制作配置和预检结果 | 是 | 是 | 是，只读 |
| 新建项目 | 是 | 是 | 否 |
| 上传素材 | 是 | 是 | 否 |
| 覆盖并保存时间线 | 是 | 是 | 否 |
| 提交渲染 | 是 | 是 | 否 |
| 查看任务、候选和母版 | 是 | 是 | 是 |
| 记录权利快照 | 沿用现有权限 | 沿用现有权限 | 否 |
| 自动采纳母版 | 否 | 否 | 否 |

前端权限仅用于体验和误操作防护；服务端现有 RBAC 仍是安全边界。

## 8. 前端临时数据结构

本批允许在新组件内部定义以下非持久化类型，不新增共享数据库或 API 模型：

`QuickCreateDraft`

- `projectMode`
- `projectId?`
- `projectName`
- `existingMediaIds[]`
- `newFiles[]`
- `clipMode`
- `fixedClipSeconds?`
- `replaceExistingTimeline`
- `confirmRender`

`QuickCreateItem`

- `localId`
- `sourceKind: EXISTING | FILE`
- `displayName`
- `status: PENDING | UPLOADING | UPLOADED | FAILED`
- `mediaId?`
- `assetVersionId?`
- `errorCode?`
- `errorMessage?`

`QuickCreateRun`

- `token`
- `projectId?`
- `phase`
- `createdProjectId?`
- `uploadedMediaIds[]`
- `taskId?`
- `startedAt`
- `lastError?`

这些对象不得包含密码、密钥、Cookie、授权头或供应商凭据。

## 9. 异常处理

| 场景 | 要求 |
|---|---|
| 401 会话过期 | 清除受保护状态并返回登录；不得继续上传或提交 |
| 403 角色不足 | 显示无权限；不得仅靠禁用按钮后继续调用 |
| 404 项目/素材不存在 | 刷新当前项目并停止 |
| 409 修订冲突 | 进入 CONFLICT，不自动覆盖 |
| 413/配额错误 | 标明文件和服务端原因，停止后续上传 |
| 上传部分成功 | 进入 PARTIAL，列出已成功的不可变版本和失败项 |
| AssetVersion 未映射 | 进入 BLOCKED |
| 权利非允许 | 进入 BLOCKED，逐素材展示原因 |
| 权利接口 5xx/网络失败 | 不按允许处理 |
| render POST 响应未知 | 刷新任务列表，不自动重复提交 |
| 任务 UNKNOWN | 显示重新查询，不显示成片 |
| 项目切换 | 使旧 token 失效，旧响应不得落入新项目 |
| 面板关闭 | 写入未开始时可取消；写入开始后仅隐藏，不声称撤销服务端事实 |

所有错误文案必须使用安全、可执行的中文信息；不得展示堆栈、令牌、内部 URL 或供应商凭据。

## 10. 模块联动

| 模块 | 本批联动 | 明确不包含 |
|---|---|---|
| M01 项目工作台 | 快速制作入口、新建/当前项目选择、修订保护 | 完整内容树、模板中心、归档删除 |
| M02 AI 漫剧 | 仅复用结构化项目概念 | 故事生成、拆镜、真实 AI 生成 |
| M04 图生视频/动作 | 仅接收已经存在的媒体素材 | 图生视频模型、动作驱动 |
| M07 智能剪辑 | 自动顺序排布、固定/原时长、一次保存 | 转场、特效、字幕识别、智能节奏 |
| M08 素材与权利 | AssetVersion 映射和权利预检 | 权利历史中心、法务审批 |
| M09 任务中心 | 规范状态、SSE、刷新和失败语义 | Attempt/Checkpoint/DeadLetter 管理台 |
| M10 成片中心 | Candidate 交接和已有母版查看 | 自动采纳、发布、撤回 |
| M03/M05/M06 | 无 | 数字人、换脸换人、变装换景 |
| M11/M12/M13 | 复用现有配额、角色和配置边界 | 计费台账、团队策略、配置发布 |

## 11. 实施文件范围

### 11.1 获批后允许修改的唯一功能文件

| 路径 | 类型 | 目的 |
|---|---|---|
| `apps/web/src/components/QuickCreatePanel.tsx` | NEW | 快速制作字段、状态机、预检与执行协调 |
| `apps/web/src/components/QuickCreatePanel.test.tsx` | NEW | 组件级状态、权限、失败和重复提交测试 |
| `apps/web/src/App.tsx` | MODIFY | 接入现有项目、上传、保存、渲染和面板定位能力 |
| `apps/web/src/index.css` | MODIFY | 面板、状态和移动端布局；不引入样式依赖 |
| `apps/web/src/i18n.ts` | MODIFY | 新增安全中文文案 |
| `apps/web/src/i18n.test.ts` | MODIFY | 文案映射回归 |
| `apps/web/src/App.test.tsx` | MODIFY | 跨组件和项目切换回归 |
| `e2e/workbench.spec.ts` | MODIFY | 浏览器工作台快速制作流程 |
| `e2e/production.spec.ts` | MODIFY | 现有真实 FFmpeg 栈上的多素材渲染与权利阻断证据 |

不得修改未列出的文件。发现必须修改其他文件时必须停止并提交 scope delta 申请。

### 11.2 明确禁止的文件和类别

- `apps/api/**`
- `apps/worker/**`
- `apps/video_use/**`
- `packages/contracts/**`
- `packages/editor/**`
- `infra/**`
- `.github/workflows/**`
- 任何 `package.json`、锁文件或 requirements 文件
- 任何迁移、数据库模型、认证、密钥、环境模板或部署文件
- 任何真实 provider/plugin/model、付费调用或生产数据文件

## 12. 验收用例

| ID | 验收场景 | 通过条件 |
|---|---|---|
| QC-01 | owner 使用 2 个已有素材 | 顺序确定，时间线无重叠，只保存一次 |
| QC-02 | editor 上传 2 个新素材 | 逐个上传、映射版本、排布、权利预检、只提交一个 render |
| QC-03 | 固定 3 秒模式 | 每个已知源素材不超过源时长，RationalTime 精确 |
| QC-04 | 当前时间线非空 | 未确认覆盖时零写入 |
| QC-05 | viewer 打开面板 | 可看不可写，网络层无 POST/PUT |
| QC-06 | 任一素材权利缺失 | 展示逐素材原因，render POST 次数为 0 |
| QC-07 | 双击主按钮 | render POST 次数严格为 1 |
| QC-08 | render 响应未知 | 先刷新任务，不自动再次 POST |
| QC-09 | 项目切换发生迟到响应 | 旧项目结果不污染新项目 |
| QC-10 | 第二个文件上传失败 | 状态为 PARTIAL，首个成功版本被如实保留 |
| QC-11 | PUT 返回 409 | 不覆盖服务端项目，要求刷新并重新预检 |
| QC-12 | task 为 UNKNOWN | 不显示下载和成功候选 |
| QC-13 | 渲染成功 | Candidate 可见，但 adoption API 调用次数为 0 |
| QC-14 | 真实浏览器多素材渲染 | Pipeline 中生成非空 MP4，顺序/时长符合计划 |
| QC-15 | 生产浏览器权利阻断 | 未允许素材无法进入渲染提交 |
| QC-16 | 范围审计 | diff 仅包含第 11.1 节文件，无依赖/后端/迁移/部署变化 |

## 13. 必须通过的校验

- Web ESLint。
- Web TypeScript 检查。
- Vite production build。
- 现有 web、contracts、editor 单元测试。
- 新增 QuickCreatePanel 和 App 回归测试。
- Playwright workbench flow。
- Docker Compose integration。
- 真实 FFmpeg 多素材渲染。
- 权利阻断浏览器证据。
- dependency/lockfile diff 必须为空。
- backend/migration/infrastructure diff 必须为空。
- `git diff --check` 或等价空白校验。

测试通过只代表仓库候选达到评审条件，不代表正式评审、合并、部署或生产商用批准。

## 14. 实施顺序和停止条件

### 14.1 实施顺序

1. 从获批时再次核验的精确 main SHA 建功能分支。
2. 先写 QuickCreatePanel 状态机和单元测试。
3. 接入 App 的现有函数，不复制 API 客户端或建立第二套状态权威。
4. 实现确定性时间线构建和并发冲突保护。
5. 实现权利预检和前端单次提交保护。
6. 增加浏览器流程与真实渲染证据。
7. 完成范围审计、测试和文档证据。
8. 提交、推送并创建草稿功能 PR；正式评审和合并另行申请。

### 14.2 立即停止条件

- 需要新增依赖或修改锁文件。
- 需要新增/修改 API、数据库模型、迁移或 Worker。
- 需要真实 provider/plugin/model、API key 或付费调用。
- 需要部署、公开访问、生产数据或生产凭据。
- 需要修改第 11.1 节以外的功能文件。
- 无法保证单次 render 提交、项目隔离或权利阻断。
- CI 失败且修复需要扩大授权范围。

## 15. 本阶段不授权事项

本审批包的编制、提交和草稿 PR 不授权：

- IM-6～IM-8 功能代码实施；
- 正式评审或合并本审批包 PR；
- 新依赖、锁文件、后端 API、模型、迁移或 Worker 修改；
- 数字人、换脸换人、变装换景；
- 真实 AI provider、模型或短视频插件；
- API key、生产凭据、付费调用或生产数据；
- 部署、域名、TLS、公开访问；
- 生产可用、商用或对外宣传；
- 独立安全、法律合规和财税专业复核的豁免。

## 16. 后续编码批准措辞

只有在本审批包正式评审并合并后，OPC 最终批准人提供以下或实质等价的明确授权，才允许进入功能编码：

> 批准按 `docs/approvals/IM6-IM8-ONE-CLICK-SHORT-VIDEO-CODING-APPROVAL.md` 第 11.1 节和第 12 节实施 IM-6～IM-8；不得修改第 11.2 节禁止范围，不得新增依赖、后端 API、模型、迁移、Worker、真实插件或模型、付费调用、部署及公开访问。完成校验后仅允许提交、推送并创建草稿功能 PR，正式评审和合并另行批准。

在该授权被明确记录前，本文件只是一份待审批的产品与工程计划，不得据此编写 IM-6～IM-8 功能代码。
