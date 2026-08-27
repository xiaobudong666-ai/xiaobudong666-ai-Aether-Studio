import { AssetVersionDTO, ProjectDTO } from "@aether/contracts";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  EditorReference,
  GenerationApiClient,
  GenerationCapabilitySnapshot,
  ServerGenerationRequest,
  ServerGenerationResult,
  ServerGenerationStatus,
  ServerGenerationTask,
} from "../generation";

interface GenerationPanelProps {
  role: "owner" | "editor" | "viewer";
  tenantId: string;
  actorId: string;
  project: ProjectDTO | null;
  assetVersions: AssetVersionDTO[];
}

const STATUS_LABEL: Record<ServerGenerationStatus, string> = {
  QUEUED: "排队中", SUBMITTING: "提交中", RUNNING: "生成中",
  INGESTING: "产物入库中", RIGHTS_BLOCKED: "等待权利审核", SUCCEEDED: "权利已允许",
  FAILED: "失败", CANCELED: "已取消", UNKNOWN: "状态待人工核对", PARTIAL: "部分完成",
};
const ACTIVE_STATUSES = new Set<ServerGenerationStatus>(["QUEUED", "SUBMITTING", "RUNNING", "INGESTING"]);

function newIdempotencyKey(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const suffix = Math.random().toString(16).slice(2).padEnd(12, "0").slice(0, 12);
  return `00000000-0000-4000-8000-${suffix}`;
}

export function GenerationPanel({ role, tenantId, actorId, project, assetVersions }: GenerationPanelProps) {
  const api = useMemo(
    () => new GenerationApiClient("/api", (input, init) => globalThis.fetch(input, init)),
    [],
  );
  const requestSequence = useRef(0);
  const idempotencyKeyRef = useRef<string | null>(null);
  const [open, setOpen] = useState(false);
  const [capabilities, setCapabilities] = useState<GenerationCapabilitySnapshot | null>(null);
  const [tasks, setTasks] = useState<ServerGenerationTask[]>([]);
  const [references, setReferences] = useState<EditorReference[]>([]);
  const [prompt, setPrompt] = useState("");
  const [aspect, setAspect] = useState<ServerGenerationRequest["videoAspect"]>("9:16");
  const [voiceName, setVoiceName] = useState("en-US-JennyNeural");
  const [concatMode, setConcatMode] = useState<ServerGenerationRequest["videoConcatMode"]>("random");
  const [clipDuration, setClipDuration] = useState(5);
  const [outputCount, setOutputCount] = useState(1);
  const [selectedAssetIds, setSelectedAssetIds] = useState<string[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [preflightBody, setPreflightBody] = useState<ServerGenerationRequest | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const generationReady = Boolean(
    capabilities?.enabled
    && !capabilities.killSwitch?.disabled
    && capabilities.circuit?.state !== "OPEN"
    && capabilities.circuit?.state !== "DISABLED"
    && (capabilities.quota?.concurrentRemaining ?? 0) > 0
    && (capabilities.quota?.monthlyRequestRemaining ?? 0) > 0
    && (capabilities.quota?.monthlyGeneratedSecondsRemaining ?? 0) > 0
  );

  const availableAssets = useMemo(
    () => assetVersions.filter((version) => version.projectId === project?.id),
    [assetVersions, project?.id],
  );
  const invalidatePreflight = () => {
    setPreflightBody(null);
    idempotencyKeyRef.current = null;
  };

  const loadServerState = useCallback(async (silent = false) => {
    const projectId = project?.id;
    const sequence = ++requestSequence.current;
    if (!projectId) {
      setTasks([]);
      setCapabilities(null);
      return;
    }
    if (!silent) setBusy(true);
    try {
      const [nextCapabilities, nextTasks] = await Promise.all([api.capabilities(), api.list(projectId)]);
      if (sequence !== requestSequence.current || projectId !== project?.id) return;
      setCapabilities(nextCapabilities);
      setTasks(nextTasks);
      if (!silent && !nextCapabilities.enabled) {
        setMessage(`生成 Provider 当前不可用：${nextCapabilities.reasonCode || "PROVIDER_DISABLED"}；既有任务仍可读取。`);
      }
    } catch (error) {
      if (sequence === requestSequence.current && !silent) setMessage(error instanceof Error ? error.message : "无法读取生成任务。");
    } finally {
      if (!silent && sequence === requestSequence.current) setBusy(false);
    }
  }, [api, project?.id]);

  useEffect(() => {
    invalidatePreflight();
    setSelectedAssetIds([]);
    setReferences([]);
    setMessage(project?.id ? "已切换项目，正在从服务端恢复任务状态。" : null);
    void loadServerState();
    return () => { requestSequence.current += 1; };
  }, [loadServerState, project?.id]);

  useEffect(() => {
    if (!open || !project?.id || !tasks.some((task) => ACTIVE_STATUSES.has(task.status))) return undefined;
    const timer = window.setInterval(() => { void loadServerState(true); }, 3000);
    return () => window.clearInterval(timer);
  }, [loadServerState, open, project?.id, tasks]);

  const buildRequest = (): ServerGenerationRequest | null => {
    if (!project || !capabilities || !confirmed) return null;
    idempotencyKeyRef.current ||= newIdempotencyKey();
    return {
      videoSubject: prompt.trim(), videoAspect: aspect, voiceName,
      videoConcatMode: concatMode, videoClipDuration: clipDuration, outputCount,
      inputAssetVersionIds: selectedAssetIds,
      idempotencyKey: idempotencyKeyRef.current,
      capabilitySnapshotHash: capabilities.snapshotHash,
      expectedProjectRevision: project.revision, confirmExternalGeneration: true,
    };
  };

  const preflight = async () => {
    const body = buildRequest();
    if (!body || !project) {
      setMessage("请填写生成主题、确认外部生成边界，并等待能力快照载入。");
      return;
    }
    setBusy(true);
    try {
      await api.validate(project.id, body);
      setPreflightBody(body);
      setMessage("服务端预检通过：可创建受治理生成任务。");
    } catch (error) {
      invalidatePreflight();
      setMessage(error instanceof Error ? error.message : "服务端预检失败。");
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    if (!project || !preflightBody || !idempotencyKeyRef.current) return;
    setBusy(true);
    try {
      const task = await api.create(project.id, preflightBody);
      setTasks((previous) => [task, ...previous.filter((candidate) => candidate.taskId !== task.taskId)]);
      setMessage(`任务 ${task.taskId} 已进入服务端队列；重复提交使用同一幂等键。`);
      invalidatePreflight();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "生成任务创建失败。");
    } finally {
      setBusy(false);
    }
  };

  const mutateTask = async (taskId: string, action: "cancel" | "retry") => {
    if (!project) return;
    setBusy(true);
    try {
      const task = action === "cancel" ? await api.cancel(project.id, taskId) : await api.retry(project.id, taskId);
      setTasks((previous) => previous.map((candidate) => candidate.taskId === taskId ? task : candidate));
      setMessage(action === "cancel" ? "取消意图已由服务端持久化。" : "已追加新的重试 attempt；历史记录保持不变。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "任务操作失败。");
    } finally {
      setBusy(false);
    }
  };

  const createEditorReference = (task: ServerGenerationTask, result: ServerGenerationResult) => {
    if (!project || !result.rights.allowed) {
      setMessage(`权利检查阻断：${result.rights.code}。不会写入剪辑引用或时间线。`);
      return;
    }
    setReferences((previous) => previous.some((reference) => reference.resultId === result.assetVersionId) ? previous : [...previous, {
      id: `editor-reference-${result.assetVersionId}`, projectId: project.id,
      assetVersionId: result.assetVersionId, resultId: result.assetVersionId,
      adopted: false, createdAt: new Date().toISOString(),
    }]);
    setMessage(`已为任务 ${task.taskId} 创建 adopted=false 的剪辑引用；未写入最终时间线、渲染或发布。`);
  };

  const renderTasks = (readOnly: boolean) => <div className="generation-task-center">
    <h3>服务端任务中心</h3>
    {tasks.length === 0 && <p>暂无生成任务。</p>}
    {tasks.map((task) => <article key={task.taskId}>
      <div><strong>{task.taskId}</strong><span>{STATUS_LABEL[task.status]} · attempt {task.attempt}/{task.maxAttempts} · {task.progress}%</span></div>
      <p>{task.message}</p>
      {!readOnly && <div className="generation-actions">
        {ACTIVE_STATUSES.has(task.status) && <button type="button" disabled={busy} onClick={() => void mutateTask(task.taskId, "cancel")}>取消</button>}
        {["FAILED", "PARTIAL"].includes(task.status) && !task.errorCode?.startsWith("NON_RETRYABLE_") && <button type="button" disabled={busy} onClick={() => void mutateTask(task.taskId, "retry")}>重试</button>}
      </div>}
      {task.errorCode && <p>错误：{task.errorCode} · {task.errorMessage}</p>}
      {task.results.map((result) => <div className="generation-result" key={result.assetVersionId}>
        <span>{result.contentType} · {result.checksum.slice(0, 12)} · {result.rights.code}</span>
        <span>来源任务：{String(result.provenance.generationTaskId || task.taskId)}</span>
        {!readOnly && <button type="button" disabled={!result.rights.allowed} onClick={() => createEditorReference(task, result)}>用于快速制作</button>}
      </div>)}
    </article>)}
  </div>;

  const renderReadiness = (compact = false) => <div className="generation-readiness" aria-label="Provider 就绪状态">
    <strong>服务端权威状态：{generationReady ? "可创建" : "已阻断"}</strong>
    {compact ? <span>
      模式 {capabilities?.operatorMode || "disabled"} · 配置 {capabilities?.ownerPolicy?.published ? "已发布" : "未发布"} · Worker {capabilities?.workerProof?.fresh ? "就绪" : "未就绪"} · 熔断 {capabilities?.circuit?.state || "CLOSED"} · 停机 {capabilities?.killSwitch?.disabled ? "是" : "否"} · 并发 {capabilities?.quota?.concurrentRemaining ?? 0}/{capabilities?.quota?.concurrentLimit ?? 0} · 月请求 {capabilities?.quota?.monthlyRequestRemaining ?? 0} · 月秒数 {capabilities?.quota?.monthlyGeneratedSecondsRemaining ?? 0}{!generationReady ? ` · ${capabilities?.reasonCode || capabilities?.killSwitch?.reasonCode || "READINESS_PENDING"}` : ""}
    </span> : <>
      <span>模式 {capabilities?.operatorMode || "disabled"} · 配置 {capabilities?.ownerPolicy?.published ? "已发布" : "未发布"} · Worker 证明 {capabilities?.workerProof?.fresh ? "新鲜" : "缺失或过期"}</span>
      <span>熔断 {capabilities?.circuit?.state || "CLOSED"} · 紧急停机 {capabilities?.killSwitch?.disabled ? "已启用" : "未启用"}</span>
      <span>并发余量 {capabilities?.quota?.concurrentRemaining ?? 0}/{capabilities?.quota?.concurrentLimit ?? 0} · 月请求余量 {capabilities?.quota?.monthlyRequestRemaining ?? 0} · 月生成秒数余量 {capabilities?.quota?.monthlyGeneratedSecondsRemaining ?? 0}</span>
      {!generationReady && <span>阻断原因：{capabilities?.reasonCode || capabilities?.killSwitch?.reasonCode || "READINESS_PENDING"}</span>}
    </>}
  </div>;

  if (role === "viewer") return <section className="generation-entry" data-tenant={tenantId} data-actor={actorId}>
    <div><strong>AI 受治理生成</strong><span>当前为只读权限；任务来自服务端，不提供创建、取消、重试或采纳操作。</span></div>
    {renderReadiness(true)}
    {renderTasks(true)}
  </section>;

  if (!open) return <section className="generation-entry" data-tenant={tenantId} data-actor={actorId}>
    <div><strong>AI 受治理生成</strong><span>任务状态由服务端持久化；真实 Provider 默认保持禁用。</span></div>
    {renderReadiness(true)}
    <button type="button" disabled={!project} onClick={() => setOpen(true)}>打开生成任务</button>
  </section>;

  return <section className="generation-panel" aria-label="AI 受治理生成任务" data-tenant={tenantId} data-actor={actorId}>
    <div className="generation-heading">
      <div><strong>IM12–IM14 · 服务端受治理生成桥接</strong><span>项目：{project?.name || "未选择"}</span></div>
      <button type="button" className="secondary" onClick={() => setOpen(false)}>收起</button>
    </div>
    {renderReadiness()}
    <div className="generation-grid">
      <fieldset disabled={busy || !generationReady}>
        <legend>生成请求</legend>
        <label>生成主题<textarea aria-label="生成主题" maxLength={500} value={prompt} onChange={(event) => { setPrompt(event.target.value); invalidatePreflight(); }} /></label>
        <label>目标比例<select aria-label="目标比例" value={aspect} onChange={(event) => { setAspect(event.target.value as ServerGenerationRequest["videoAspect"]); invalidatePreflight(); }}>{(capabilities?.videoAspects || ["9:16"]).map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>声音<select aria-label="声音" value={voiceName} onChange={(event) => { setVoiceName(event.target.value); invalidatePreflight(); }}>{(capabilities?.voices || [voiceName]).map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>拼接模式<select aria-label="拼接模式" value={concatMode} onChange={(event) => { setConcatMode(event.target.value as ServerGenerationRequest["videoConcatMode"]); invalidatePreflight(); }}><option value="random">随机</option><option value="sequential">顺序</option></select></label>
        <label>单片段时长（秒）<input aria-label="单片段时长" type="number" min="1" max="10" value={clipDuration} onChange={(event) => { setClipDuration(Number(event.target.value)); invalidatePreflight(); }} /></label>
        <label>输出数量<input aria-label="输出数量" type="number" min="1" max={capabilities?.maxOutputs || 1} value={outputCount} onChange={(event) => { setOutputCount(Number(event.target.value)); invalidatePreflight(); }} /></label>
      </fieldset>
      <fieldset disabled={busy || !generationReady}>
        <legend>参考素材与确认</legend>
        {availableAssets.length === 0 && <span>当前项目暂无可选素材版本。</span>}
        {availableAssets.map((version) => <label key={version.id}><input type="checkbox" checked={selectedAssetIds.includes(version.id)} onChange={(event) => { setSelectedAssetIds((previous) => event.target.checked ? [...previous, version.id] : previous.filter((id) => id !== version.id)); invalidatePreflight(); }} />版本 {version.versionNo} · {version.sha256.slice(0, 12)}</label>)}
        <label><input aria-label="确认外部生成边界" type="checkbox" checked={confirmed} onChange={(event) => { setConfirmed(event.target.checked); invalidatePreflight(); }} />确认任务只进入受治理队列，结果默认受权利阻断。</label>
        <p>Provider：{capabilities?.mode || "载入中"} · {capabilities?.sourceVersion || "未知"}</p>
      </fieldset>
      <fieldset>
        <legend>预检与提交</legend>
        <button type="button" disabled={busy || !generationReady || !prompt.trim() || !confirmed} onClick={() => void preflight()}>执行服务端预检</button>
        <button type="button" disabled={busy || !preflightBody} onClick={() => void submit()}>提交生成</button>
        <button type="button" className="secondary" disabled={busy || !project} onClick={() => void loadServerState()}>刷新任务</button>
        <p>状态：{preflightBody ? "预检通过" : "等待预检"}</p>
        {message && <div role="status">{message}</div>}
      </fieldset>
    </div>
    {renderTasks(false)}
    {references.length > 0 && <p className="generation-boundary">已创建 {references.length} 个受治理引用；全部 adopted=false，未写入最终时间线。</p>}
  </section>;
}
