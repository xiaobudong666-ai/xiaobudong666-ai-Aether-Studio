import { useEffect, useMemo, useRef, useState } from "react";
import { AssetVersionDTO, ProjectDTO } from "@aether/contracts";
import {
  DeterministicGenerationAdapter,
  EditorReference,
  GenerationInput,
  GenerationTask,
  preflightGeneration,
} from "../generation";

interface GenerationPanelProps {
  role: "owner" | "editor" | "viewer";
  tenantId: string;
  actorId: string;
  project: ProjectDTO | null;
  assetVersions: AssetVersionDTO[];
}

const STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿", PREFLIGHT: "预检通过", BLOCKED: "已阻断", QUEUED: "排队中",
  RUNNING: "生成中", SUCCEEDED: "已完成", FAILED: "失败", CANCELLED: "已取消", EXPIRED: "已过期",
};

export function GenerationPanel({ role, tenantId, actorId, project, assetVersions }: GenerationPanelProps) {
  const adapterRef = useRef(new DeterministicGenerationAdapter());
  const [open, setOpen] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [aspectRatio, setAspectRatio] = useState<GenerationInput["aspectRatio"]>("9:16");
  const [durationSeconds, setDurationSeconds] = useState(10);
  const [outputCount, setOutputCount] = useState(1);
  const [selectedAssetIds, setSelectedAssetIds] = useState<string[]>([]);
  const [rightsSnapshotId, setRightsSnapshotId] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [preflightReady, setPreflightReady] = useState(false);
  const [tasks, setTasks] = useState<GenerationTask[]>([]);
  const [references, setReferences] = useState<EditorReference[]>([]);
  const clientRequestIdRef = useRef<string | null>(null);

  useEffect(() => {
    setPreflightReady(false);
    setMessage(project?.id ? "项目已切换，请重新预检生成参数。" : null);
    setSelectedAssetIds([]);
    clientRequestIdRef.current = null;
  }, [project?.id]);

  const availableAssets = useMemo(() => assetVersions.filter((version) => version.projectId === project?.id), [assetVersions, project?.id]);
  const input = (): GenerationInput => ({
    tenantId,
    projectId: project?.id || "",
    prompt,
    inputAssetIds: selectedAssetIds,
    aspectRatio,
    durationMs: durationSeconds * 1000,
    outputCount,
    rightsSnapshotIds: rightsSnapshotId.trim() ? [rightsSnapshotId.trim()] : [],
    role,
    quotaAvailable: true,
    expectedRevision: project?.revision,
    currentRevision: project?.revision,
  });

  const refresh = () => setTasks(adapterRef.current.list());

  const preflight = () => {
    const result = preflightGeneration(input());
    setPreflightReady(result.allowed);
    setMessage(result.allowed ? "预检通过：可提交本地确定性生成任务。" : `预检阻断：${result.errors.join("、")}`);
    if (result.allowed && !clientRequestIdRef.current) {
      clientRequestIdRef.current = `generation-${project?.id}-${Date.now()}`;
    }
  };

  const submit = () => {
    if (!preflightReady || !clientRequestIdRef.current) return;
    try {
      const task = adapterRef.current.submit(input(), clientRequestIdRef.current, actorId);
      refresh();
      setMessage(`任务 ${task.id} 已进入本地队列；重复点击不会创建第二个任务。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "任务提交失败。");
    }
  };

  const run = (task: GenerationTask) => {
    adapterRef.current.start(task.id, actorId);
    adapterRef.current.complete(task.id, task.attempt, tenantId, project?.id || "", actorId);
    refresh();
    setMessage("本地确定性任务完成，结果等待人工审阅。未写入最终时间线。");
  };

  const cancel = (taskId: string) => {
    adapterRef.current.cancel(taskId, actorId);
    refresh();
  };

  const retry = (taskId: string) => {
    adapterRef.current.retry(taskId, actorId);
    refresh();
  };

  const enterEditor = (task: GenerationTask, resultId: string) => {
    try {
      const reference = adapterRef.current.reviewResult(task.id, resultId, tenantId, project?.id || "", actorId);
      setReferences((previous) => previous.some((item) => item.id === reference.id) ? previous : [...previous, reference]);
      setMessage("已创建受治理的素材版本引用；未自动采纳，也未写入最终时间线。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "结果审阅失败。");
    }
  };

  if (role === "viewer") {
    return <section className="generation-entry"><div><strong>AI 受治理生成</strong><span>当前为只读权限，可查看任务但不能创建、取消、重试或采纳。</span></div></section>;
  }

  if (!open) {
    return (
      <section className="generation-entry">
        <div><strong>AI 受治理生成</strong><span>本阶段仅使用 deterministic fake/local adapter，不调用真实模型。</span></div>
        <button type="button" disabled={!project} onClick={() => setOpen(true)}>打开生成任务</button>
      </section>
    );
  }

  return (
    <section className="generation-panel" aria-label="AI 受治理生成任务">
      <div className="generation-heading">
        <div><strong>IM9–IM11 · 生成请求、任务中心与结果审阅</strong><span>项目：{project?.name || "未选择"}</span></div>
        <button type="button" className="secondary" onClick={() => setOpen(false)}>收起</button>
      </div>
      <div className="generation-grid">
        <fieldset>
          <legend>生成请求</legend>
          <label>提示词<textarea aria-label="生成提示词" value={prompt} onChange={(event) => { setPrompt(event.target.value); setPreflightReady(false); }} /></label>
          <label>目标比例<select aria-label="目标比例" value={aspectRatio} onChange={(event) => { setAspectRatio(event.target.value as GenerationInput["aspectRatio"]); setPreflightReady(false); }}><option>9:16</option><option>16:9</option><option>1:1</option></select></label>
          <label>时长（秒）<input aria-label="生成时长" type="number" min="1" max="60" value={durationSeconds} onChange={(event) => { setDurationSeconds(Number(event.target.value)); setPreflightReady(false); }} /></label>
          <label>输出数量<input aria-label="输出数量" type="number" min="1" max="4" value={outputCount} onChange={(event) => { setOutputCount(Number(event.target.value)); setPreflightReady(false); }} /></label>
          <label>权利快照编号<input aria-label="权利快照编号" value={rightsSnapshotId} onChange={(event) => { setRightsSnapshotId(event.target.value); setPreflightReady(false); }} placeholder="必填，例如 rights-123" /></label>
        </fieldset>
        <fieldset>
          <legend>参考素材（可选）</legend>
          {availableAssets.length === 0 && <span>当前项目暂无可引用素材版本。</span>}
          {availableAssets.map((version) => (
            <label key={version.id}><input type="checkbox" aria-label={`参考素材 ${version.id}`} checked={selectedAssetIds.includes(version.id)} onChange={(event) => { setSelectedAssetIds((previous) => event.target.checked ? [...previous, version.id] : previous.filter((id) => id !== version.id)); setPreflightReady(false); }} />版本 {version.versionNo} · {version.sha256.slice(0, 12)}</label>
          ))}
          <p>模型能力：本地确定性占位器</p>
          <p>不会访问第三方 API、不会产生费用。</p>
        </fieldset>
        <fieldset>
          <legend>预检与提交</legend>
          <button type="button" onClick={preflight}>执行生成预检</button>
          <button type="button" disabled={!preflightReady} onClick={submit}>提交生成</button>
          <p>状态：{preflightReady ? "预检通过" : "等待预检"}</p>
          {message && <div role="status">{message}</div>}
        </fieldset>
      </div>
      <div className="generation-task-center">
        <h3>任务中心</h3>
        {tasks.length === 0 && <p>暂无生成任务。</p>}
        {tasks.map((task) => (
          <article key={task.id}>
            <div><strong>{task.id}</strong><span>{STATUS_LABEL[task.status] || task.status} · attempt {task.attempt} · {task.progress}%</span></div>
            <div className="generation-actions">
              {task.status === "QUEUED" && <button type="button" onClick={() => run(task)}>运行本地任务</button>}
              {["QUEUED", "RUNNING"].includes(task.status) && <button type="button" onClick={() => cancel(task.id)}>取消</button>}
              {task.status === "FAILED" && !task.errorCode?.startsWith("NON_RETRYABLE_") && <button type="button" onClick={() => retry(task.id)}>重试</button>}
            </div>
            {task.results.map((result) => (
              <div className="generation-result" key={result.id}>
                <span>{result.mimeType} · {result.width}×{result.height} · {result.checksum.slice(0, 12)}</span>
                <span>来源：{result.provenance}</span>
                <button type="button" onClick={() => enterEditor(task, result.id)}>进入剪辑</button>
              </div>
            ))}
          </article>
        ))}
      </div>
      {references.length > 0 && <p className="generation-boundary">已创建 {references.length} 个受治理引用；全部 adopted=false，未写入最终时间线。</p>}
    </section>
  );
}
