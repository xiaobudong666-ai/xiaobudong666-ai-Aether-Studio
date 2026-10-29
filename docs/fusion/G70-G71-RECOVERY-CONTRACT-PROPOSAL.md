# G70/G71 受治理恢复与重新关联契约（提案，仅供 Owner 审核）

- **项目架构：** FUSION-M0 V7.04 Option A（不变）
- **文档状态：** DRAFT / PROPOSAL ONLY；不是权威 Master 更新，不授权实现、合并或部署
- **代码依据：** Aether main `15382529994df4ee28758244ea7c28b32168ea9e`；PR #58 head `fcc4d39b0f866d775c0b2e932e41b98b3e9bda39`；PR #60 head `a73f76396ef9d210a4f293978cf0f7efeac1d827`
- **目的：** 把当前只读核验与安全重试阻断，推进到可由 Owner 审核的恢复事务契约；不提前决定 M11 结算或任务终态政策。

## 1. 已证实的当前行为（不是推断）

PR #60 在 `apps/api/app/main.py` 提供只读端点：

`GET /projects/{project_id}/generation-tasks/{task_id}/artifact-recovery-preflight`

当前端点要求 Owner 角色、同租户任务，并检查任务为 `FAILED / ARTIFACT_INGEST_FAILED`、尚无 `asset_version_id`；从失败事件读取原始媒资 ID、SHA-256、字节数、探测元数据及原始 upstream/provider artifact ID，向 video-use 读取现有对象并逐字节重新计算摘要。成功只返回 `VERIFIED_NOT_RECOVERED`、`recoveryPerformed=false`、`quotaMutationPerformed=false`、`settlementPerformed=false`。

PR #60 的精确提交 CI Run #503 已通过；测试也验证了在上传流中断且尚未生成媒资 ID 时，人工及重复重试均返回 `409 ARTIFACT_RECOVERY_REQUIRED`，不新增 attempt/event/usage/SETTLED，不改变存储额度，且不调用 Provider。

这些证据证明的是**只读核验与失败关闭**。当前没有恢复提交接口，也没有恢复/重建机制。

## 2. 必须分开的两种失败

| 场景 | 可验证事实 | 提案处理边界 |
|---|---|---|
| A. video-use 已返回 media ID，随后 API 数据库提交失败 | 失败事件可能保存 media ID、原始 artifact/upstream ID、字节数及 SHA-256；可尝试只读核验既存对象 | 只有完整绑定和精确字节校验通过，才可进入未来受治理提交评估；预检本身不关联资产、不改额度、不结算 |
| B. 上传流在 video-use 返回 media ID 之前中断 | 不存在已知可寻址的 sidecar media ID | 保持当前失败关闭；不虚构 ID、不把任务改成可重试生成、不重新调用 Provider。恢复来源未解决前不得进入恢复提交 |

场景 B 不能复用场景 A 的 preflight 结论。不得把“原始 Provider artifact ID 仍存在”当作 sidecar 对象已经可取回的证据。

## 3. 未来恢复提交的前置条件（均为提案）

以下只定义待审核契约，不创建端点、不修改状态机：

1. **可信来源绑定：** task、tenant、project、attempt、upstream job、provider artifact、media ID、SHA-256、字节数及原始失败事件必须互相一致。任何关键字段缺失、冲突或由客户端自行声明，都失败关闭。
2. **精确预检：** 只允许对已存在的 sidecar 对象做读取和字节/摘要核验；不存在对象、长度超限、摘要不符、源不可用时不得产生部分业务写入。
3. **提交前门禁：** 需有 Owner 明确批准的恢复事务契约和 M11 可信结算规则。不得根据合成探测元数据或客户端提供的时长、费用、租户、操作者身份直接生成财务结算。
4. **单一受治理提交：** 资产版本、项目素材清单/修订号、任务关联、事件审计和用量/结算处理之间必须有明确的一致性设计。由于 sidecar 存储与关系数据库不共享事务，需在实现前设计可恢复的事务/意图记录与对账流程；不能声称跨系统原子性。
5. **乐观并发控制：** 以任务状态/版本和项目 revision 做 compare-and-swap；重复提交不能创建第二个资产版本、第二条结算或重复扣减。数据库层唯一约束及并发测试需在实现设计中明确。
6. **幂等语义：** 恢复请求使用独立幂等键并绑定规范化请求摘要。相同键、相同请求返回同一既有回执；相同键、不同请求拒绝；ACK 不明时先查询持久结果，不重新生成、不重复结算。
7. **失败补偿：** 在任一阶段发生超时、进程崩溃、数据库冲突、源对象变化或持久化结果不明时，必须保留可审计证据并进入明确的待核验状态；不得自动释放/结算额度，除非已批准的 M11 规则明确规定。
8. **禁止 Provider 副作用：** 恢复/重新关联路径只能处理原始产物；不得触发新的生成任务、真实 Provider 调用、付费重投或凭据读取。

## 4. Owner 本人、管理员代操作与租户隔离

- **Owner 本人：** 必须由服务端会话确定身份，并验证其属于任务所在租户且对项目具有 Owner 权限；不接受请求体中的 `actorId`、`tenantId` 或自报角色作为授权依据。
- **管理员代操作：** 管理员角色本身不等于 Owner 委托。未来若允许代操作，必须由已批准的独立能力授予记录证明，至少绑定 tenant、project、task/操作范围、授权主体、被委托主体、签发者/epoch、有效期及撤销状态。没有有效能力时拒绝；不允许冒充 Owner。
- **跨租户访问：** 对外资源查询遵循现有租户隐藏规则；不泄露其他租户的任务、媒资 ID、摘要、事件或恢复状态。负例须验证数据库无任何写入。
- **审计：** 记录实际操作者、有效权限/委托证据 ID、原因码、目标 task/attempt/project、恢复请求 ID、原始/最终摘要、提交结果和时间。不得记录 Provider 密钥或其他秘密。

## 5. 提案状态与错误处理矩阵

| 条件 | 当前/建议行为 | 业务写入 |
|---|---|---|
| 无 media ID（场景 B） | 当前 `409 ARTIFACT_RECOVERY_REQUIRED`；保持不变，直至有已批准的恢复来源与政策 | 不写入 |
| 已有对象且摘要完全匹配 | 当前仅 `VERIFIED_NOT_RECOVERED`；未来也必须再经过独立提交门禁 | 预检不写入 |
| 对象缺失/sidecar 不可用 | 当前 `503 ARTIFACT_RECOVERY_SOURCE_UNAVAILABLE` 或既有相应错误 | 不写入 |
| 摘要/字节数/绑定不符 | 当前相应 `409 ARTIFACT_RECOVERY_EVIDENCE_MISMATCH` 或 `ARTIFACT_RECOVERY_EVIDENCE_INVALID` | 不写入 |
| 资源属于其他租户 | 隐藏资源并拒绝 | 不写入 |
| 管理员无明确有效委托 | 拒绝；不得按角色自动升级 | 不写入 |
| 同幂等键并发/重复提交 | 未来应返回同一持久结果或明确冲突；不得重复创建资产/结算 | 最多一次 |
| 结算证据不可信或 M11 规则未批准 | 不进入财务 READY/SETTLED | 不写入结算 |

上表中“未来应”是待审核设计要求，不代表当前实现已经具备该行为。

## 6. 必需的未来验收批次

在 Owner 批准契约后，才可实施相应代码与测试。至少覆盖：

- Owner 同租户合法恢复：真实非生产 API + 数据库 + sidecar；核对任务/资产/项目 revision/事件/用量一致性。
- 已有 media ID 的正确字节、错误 SHA、错误长度、源对象缺失、源不可用。
- 无 media ID 的中断：人工及重复重试保持阻断；Provider 调用计数为零。
- 同租户非 Owner、无委托管理员、过期/撤销委托、错误 project/task 范围。
- 跨租户 task/media ID 猜测：返回隐藏/拒绝且所有相关表无写入。
- 同幂等键重复与并发提交、不同 payload 复用幂等键、项目 revision 冲突、已有资产重复关联。
- 在 sidecar 核验后、数据库提交前后、回执前模拟进程终止；重启后查询持久记录，不重复创建资产或结算。
- M11 用量/结算负例：缺失或不可信证据不得进入 `SETTLED`；预留、释放、结算总账与存储用量按已批准规则核对。
- 在 PostgreSQL 多事务/多 Worker 环境做并发和恢复验证；SQLite/单进程 CI 不能替代该证据。

每个用例必须记录精确 Git SHA、环境、命令、退出码、关键日志/数据库断言及 Provider 调用证据。只通过静态检查或离线单测不得关闭业务门禁。

## 7. 既有精确提交 CI 的命令与结果（不是本提案的新测试）

证据对象是 PR #60 的提交 `a73f76396ef9d210a4f293978cf0f7efeac1d827`，GitHub Actions Run #503（ID `40911128564`），结果 `completed / success`。本节保存该次 workflow 的命令范围；本次新增的是文档，不声称重新执行这些命令。

Workflow 文件：`.github/workflows/ci.yml`，读取版本为 PR #60 head；其 Blob SHA 为 `c214752af40e91963b9c802954787c179c105141`。

- Web/package：`pnpm test` — PASS。
- API：环境 `PYTHONPATH=apps/api`；`python -m pytest apps/api/test_main.py apps/api/test_moneyprinter.py apps/api/test_generation_tasks.py apps/api/test_runtime_gates.py apps/api/test_detection_evidence.py -v` — PASS；PR #60 精确提交记录为 136 passed、1 warning。
- FAKE-only private-canary：`bash -n infra/docker/provider-canary.sh` 和 `python infra/docker/provider-canary-smoke.py self-test` — PASS。
- Node 生产依赖审计：`pnpm audit --prod` — PASS。
- Python 依赖审计：`pip-audit -r apps/api/requirements.txt`、`pip-audit -r apps/worker/requirements.txt`、`pip-audit -r apps/video_use/requirements.txt` — PASS。
- Worker 容器：workflow 将测试文件复制到临时路径后执行 `python -m pytest /tmp/test_worker.py /tmp/test_moneyprinter.py /tmp/test_generation_tasks.py /tmp/test_deterministic_fake_adapter.py /tmp/test_talking_head_provider_adapter.py /tmp/test_talking_head_runtime.py /tmp/test_talking_head_arm_disarm.py /tmp/test_talking_head_egress_proxy.py -v` — Docker Compose job PASS。
- video-use 容器：`python -m pytest /tmp/test_main.py -v` — Docker Compose job PASS。
- 浏览器流程：`pnpm e2e` — PASS；Docker 集成中的上传到下载浏览器用例 3 passed。
- Docker Compose：构建并启动隔离服务，执行 Worker/video-use 测试、合成 FFmpeg 渲染、认证上传/持久队列/Worker/媒资处理/下载流程，并清理本次运行创建的容器、卷和网络 — PASS。

Run #503 的六个必需 job 全部成功：Node production dependency audit、Core lint/build/API/unit、Python dependency audit、必需门禁聚合、Playwright workbench、Docker Compose integration。没有在此处把该 CI 扩展解释为 PostgreSQL 多 Worker 证明，也没有把它解释为恢复提交或 M11 结算已实现。

## 8. 仍待 Owner 决策的 8 项条件政策（原样保留）

1. M11 跨账期首次释放的归属。
2. 口播 `outputCount=2..4` 的成片与计费语义。
3. 历史 `territory=GLOBAL` 权限解释与存量权限迁移。
4. M11 失败渲染计入当月额度还是返还（包括重试、取消、远端结果不明）。
5. 历史渲染任务缺少输入资产版本清单时，采用禁止、隔离补证还是重新渲染。
6. M11 正式账单允许的 FX 证据来源及最大容差。
7. `AGGREGATE_CONTAINS_RUNTIME` 子集差异是否可在额外用量/密钥/租户归属补证后进入财务 READY，以及残差证据标准。
8. M12 正式 EXPORT 授权的独立证据/签发者、Owner/editor 自证是否允许，以及历史缺证 ALLOWED 快照如何迁移。

本提案不回答、不覆盖、不隐式批准上述任何决策。

## 9. 关闭标准与当前审批状态

G70/G71 只有在以下全部满足后才可按 Owner 统一清单申请正式关闭：恢复来源/绑定契约获批；恢复提交实现经过代码评审；M11 规则获批且事务一致性有证据；Owner/委托管理员/跨租户负例通过；并发、幂等、重启和故障注入测试通过；精确提交的完整非生产 CI 通过；证据已关联至统一验收清单。

**当前状态：** 仅提交本提案文档；没有实现恢复提交接口，没有执行新的业务测试，没有修改生产代码、额度/结算政策或状态机。PR #60 的 Run #503 只证明其现有精确提交测试通过。主线不变；不合并、不部署、不调用真实 Provider。
