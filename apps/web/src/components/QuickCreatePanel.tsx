import { useEffect, useMemo, useRef, useState } from "react";
import {
  AssetVersionDTO,
  MaterialDTO,
  ProjectDTO,
  RenderTaskDTO,
  RightsCheckDTO,
  TimelineDTO,
  TimelineSchema,
} from "@aether/contracts";
import { quickCreatePhaseLabel, rightsDecisionLabel, safeErrorMessage } from "../i18n";

export type QuickCreatePhase =
  | "IDLE" | "EDITING" | "VALIDATING" | "READY" | "CREATING_PROJECT"
  | "UPLOADING" | "RIGHTS_CHECKING" | "ARRANGING" | "SAVING"
  | "SUBMITTING" | "TRACKING" | "BLOCKED" | "PARTIAL" | "CONFLICT"
  | "AMBIGUOUS" | "FAILED";

type DraftItem = {
  key: string;
  kind: "existing" | "file";
  name: string;
  mediaId?: string;
  file?: File;
};

type PreparedItem = DraftItem & {
  mediaId: string;
  assetVersionId: string;
  versionNo: number;
  sha256: string;
};

type RightsResult = PreparedItem & { check: RightsCheckDTO };

export interface QuickProjectSnapshot {
  project: ProjectDTO;
  assetVersions: AssetVersionDTO[];
}

export interface QuickUploadResult {
  project: ProjectDTO;
  assetVersion: AssetVersionDTO;
}

interface QuickCreatePanelProps {
  role: "owner" | "editor" | "viewer";
  currentProject: ProjectDTO | null;
  assetVersions: AssetVersionDTO[];
  busy: boolean;
  createProject: (name: string) => Promise<QuickProjectSnapshot>;
  reloadProject: (projectId: string) => Promise<QuickProjectSnapshot>;
  uploadMedia: (projectId: string, expectedRevision: number, file: File) => Promise<QuickUploadResult>;
  checkRights: (projectId: string, assetVersionId: string) => Promise<RightsCheckDTO>;
  saveTimeline: (project: ProjectDTO, timeline: TimelineDTO) => Promise<ProjectDTO>;
  submitRender: (projectId: string) => Promise<RenderTaskDTO>;
  refreshRenderTasks: (projectId: string) => Promise<RenderTaskDTO[]>;
  isProjectActive: (projectId: string) => boolean;
  onOpenGovernance: () => void;
  onViewTask: () => void;
  onViewFinished: () => void;
}

const MAX_ITEMS = 20;
const TIMESCALE = 24000;

function latestVersion(versions: AssetVersionDTO[], mediaId: string) {
  return versions
    .filter((version) => version.mediaId === mediaId)
    .sort((left, right) => right.versionNo - left.versionNo)[0];
}

function prepareItems(
  items: DraftItem[],
  project: ProjectDTO,
  versions: AssetVersionDTO[],
): PreparedItem[] {
  return items.map((item) => {
    const mediaId = item.mediaId;
    if (!mediaId || !project.materials.some((material) => material.id === mediaId)) {
      throw new Error(`素材“${item.name}”未能映射到当前项目。`);
    }
    const version = latestVersion(versions, mediaId);
    if (!version) throw new Error(`素材“${item.name}”缺少可用版本治理记录。`);
    return {
      ...item,
      mediaId,
      assetVersionId: version.id,
      versionNo: version.versionNo,
      sha256: version.sha256,
    };
  });
}

function buildTimeline(
  items: PreparedItem[],
  materials: MaterialDTO[],
  clipMode: "ORIGINAL_DURATION" | "FIXED_DURATION",
  fixedClipSeconds: number,
  runToken: string,
): TimelineDTO {
  let start = 0;
  const trackId = `quick-track-${runToken}`;
  const clips = items.map((item, index) => {
    const material = materials.find((candidate) => candidate.id === item.mediaId);
    if (!material) throw new Error(`素材“${item.name}”不在当前项目中。`);
    let sourceFrames: number;
    if (material.duration) {
      sourceFrames = Math.max(1, Math.round(
        (material.duration.value / material.duration.timescale) * TIMESCALE,
      ));
    } else if (material.type === "image") {
      sourceFrames = 3 * TIMESCALE;
    } else {
      throw new Error(`素材“${item.name}”缺少可用时长，无法自动排布。`);
    }
    const duration = clipMode === "FIXED_DURATION"
      ? Math.min(sourceFrames, fixedClipSeconds * TIMESCALE)
      : sourceFrames;
    const clip = {
      id: `quick-clip-${runToken}-${index + 1}`,
      trackId,
      materialId: item.mediaId,
      start: { value: start, timescale: TIMESCALE },
      duration: { value: duration, timescale: TIMESCALE },
      sourceIn: { value: 0, timescale: TIMESCALE },
      volume: 1,
      opacity: 1,
      x: 0,
      y: 0,
    };
    start += duration;
    return clip;
  });
  return TimelineSchema.parse({
    version: "1.1",
    tracks: [{ id: trackId, name: "视频轨道 1", type: "video", clips }],
  });
}

export function QuickCreatePanel({
  role,
  currentProject,
  assetVersions,
  busy,
  createProject,
  reloadProject,
  uploadMedia,
  checkRights,
  saveTimeline,
  submitRender,
  refreshRenderTasks,
  isProjectActive,
  onOpenGovernance,
  onViewTask,
  onViewFinished,
}: QuickCreatePanelProps) {
  const canEdit = role === "owner" || role === "editor";
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<QuickCreatePhase>("IDLE");
  const [projectMode, setProjectMode] = useState<"NEW_PROJECT" | "CURRENT_PROJECT">(
    currentProject ? "CURRENT_PROJECT" : "NEW_PROJECT",
  );
  const [projectName, setProjectName] = useState("");
  const [items, setItems] = useState<DraftItem[]>([]);
  const [clipMode, setClipMode] = useState<"ORIGINAL_DURATION" | "FIXED_DURATION">("ORIGINAL_DURATION");
  const [fixedClipSeconds, setFixedClipSeconds] = useState(3);
  const [replaceExistingTimeline, setReplaceExistingTimeline] = useState(false);
  const [confirmRender, setConfirmRender] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [rights, setRights] = useState<RightsResult[]>([]);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [taskStatus, setTaskStatus] = useState<string | null>(null);
  const [resume, setResume] = useState<{ projectId: string; items: PreparedItem[] } | null>(null);
  const runTokenRef = useRef(0);
  const submittingRef = useRef(false);
  const operationStageRef = useRef<QuickCreatePhase>("IDLE");
  const initialProjectIdRef = useRef(currentProject?.id || null);

  useEffect(() => {
    const nextId = currentProject?.id || null;
    if (initialProjectIdRef.current === nextId) return;
    initialProjectIdRef.current = nextId;
    if (submittingRef.current) return;
    runTokenRef.current += 1;
    submittingRef.current = false;
    setProjectMode(nextId ? "CURRENT_PROJECT" : "NEW_PROJECT");
    setPhase(open ? "EDITING" : "IDLE");
    setItems([]);
    setRights([]);
    setResume(null);
    setTaskId(null);
    setTaskStatus(null);
    setMessage(nextId ? "项目已切换，请重新选择素材并预检。" : null);
  }, [currentProject?.id, open]);

  const hasExistingTimeline = Boolean(currentProject?.timeline.tracks.some(
    (track) => track.clips.length > 0,
  ));
  const existingMediaIds = useMemo(
    () => new Set(items.filter((item) => item.kind === "existing").map((item) => item.mediaId)),
    [items],
  );
  const working = !["IDLE", "EDITING", "READY", "BLOCKED", "PARTIAL", "CONFLICT", "AMBIGUOUS", "FAILED", "TRACKING"].includes(phase);

  const resetDraft = () => {
    runTokenRef.current += 1;
    submittingRef.current = false;
    setOpen(false);
    setPhase("IDLE");
    setItems([]);
    setRights([]);
    setResume(null);
    setTaskId(null);
    setTaskStatus(null);
    setMessage(null);
    setProjectName("");
    setConfirmRender(false);
    setReplaceExistingTimeline(false);
  };

  const addExisting = (material: MaterialDTO, checked: boolean) => {
    setPhase("EDITING");
    setRights([]);
    setResume(null);
    setItems((previous) => checked
      ? previous.length >= MAX_ITEMS || previous.some((item) => item.mediaId === material.id)
        ? previous
        : [...previous, { key: `existing-${material.id}`, kind: "existing", name: material.name, mediaId: material.id }]
      : previous.filter((item) => item.mediaId !== material.id));
  };

  const addFiles = (files: FileList | null) => {
    if (!files) return;
    const candidates = Array.from(files);
    setItems((previous) => [
      ...previous,
      ...candidates.slice(0, Math.max(0, MAX_ITEMS - previous.length)).map((file, index) => ({
        key: `file-${Date.now()}-${index}-${file.name}`,
        kind: "file" as const,
        name: file.name,
        file,
      })),
    ]);
    setPhase("EDITING");
    setRights([]);
    setResume(null);
  };

  const moveItem = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= items.length) return;
    setItems((previous) => {
      const next = [...previous];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
    setPhase("EDITING");
  };

  const validateDraft = () => {
    if (!canEdit) throw new Error("当前为只读权限，不能执行快速制作。");
    if (items.length < 1 || items.length > MAX_ITEMS) throw new Error("请选择 1～20 个素材。");
    if (projectMode === "NEW_PROJECT" && (projectName.trim().length < 1 || projectName.trim().length > 120)) {
      throw new Error("新项目名称须为 1～120 个字符。");
    }
    if (projectMode === "CURRENT_PROJECT" && !currentProject) throw new Error("请先选择当前项目。");
    if (projectMode === "NEW_PROJECT" && items.some((item) => item.kind === "existing")) {
      throw new Error("新建项目不能使用当前项目的既有素材，请移除后重试。");
    }
    if (clipMode === "FIXED_DURATION" && (!Number.isInteger(fixedClipSeconds) || fixedClipSeconds < 1 || fixedClipSeconds > 30)) {
      throw new Error("统一片段时长须为 1～30 秒整数。");
    }
    if (projectMode === "CURRENT_PROJECT" && hasExistingTimeline && !replaceExistingTimeline) {
      throw new Error("当前时间线已有片段，请明确确认覆盖后再继续。");
    }
  };

  const readRights = async (projectId: string, prepared: PreparedItem[], token: number) => {
    setPhase("RIGHTS_CHECKING");
    const results: RightsResult[] = [];
    for (const item of prepared) {
      const check = await checkRights(projectId, item.assetVersionId);
      if (runTokenRef.current !== token || !isProjectActive(projectId)) {
        throw new Error("活动项目已变化，本次结果已丢弃，请重新预检。");
      }
      results.push({ ...item, check });
    }
    setRights(results);
    return results;
  };

  const preflight = async () => {
    const token = ++runTokenRef.current;
    setMessage(null);
    setRights([]);
    setPhase("VALIDATING");
    try {
      validateDraft();
      if (projectMode === "CURRENT_PROJECT" && currentProject) {
        const existing = items.filter((item) => item.kind === "existing");
        const prepared = prepareItems(existing, currentProject, assetVersions);
        const results = await readRights(currentProject.id, prepared, token);
        const blocked = results.filter((result) => !result.check.allowed);
        if (blocked.length > 0) {
          setPhase("BLOCKED");
          setMessage("既有素材权利预检未通过；请在素材治理中处理后重新预检。");
          return;
        }
      }
      if (runTokenRef.current !== token) return;
      setPhase("READY");
      setMessage(items.some((item) => item.kind === "file")
        ? "既有素材预检通过；新文件须上传取得版本后再检查权利。"
        : "预检通过，可以确认并生成短视频。");
    } catch (error) {
      if (runTokenRef.current !== token) return;
      setPhase("BLOCKED");
      setMessage(safeErrorMessage(error, "预检失败，请检查输入后重试。"));
    }
  };

  const arrangeSaveAndRender = async (
    snapshot: QuickProjectSnapshot,
    prepared: PreparedItem[],
    token: number,
  ) => {
    const latestTimelineHasClips = snapshot.project.timeline.tracks.some((track) => track.clips.length > 0);
    if (latestTimelineHasClips && !replaceExistingTimeline) {
      throw new Error(`项目版本 ${snapshot.project.revision} 的时间线已有片段，请确认覆盖并重新预检。`);
    }
    operationStageRef.current = "RIGHTS_CHECKING";
    const results = await readRights(snapshot.project.id, prepared, token);
    const blocked = results.filter((result) => !result.check.allowed);
    if (blocked.length > 0) {
      setResume({ projectId: snapshot.project.id, items: prepared });
      setPhase("BLOCKED");
      setMessage("权利检查未通过：未保存时间线，也未提交渲染。请完成素材治理后继续。");
      return;
    }
    setPhase("ARRANGING");
    operationStageRef.current = "ARRANGING";
    const timeline = buildTimeline(
      prepared,
      snapshot.project.materials,
      clipMode,
      fixedClipSeconds,
      String(token),
    );
    if (runTokenRef.current !== token || !isProjectActive(snapshot.project.id)) {
      throw new Error("活动项目已变化，本次时间线未保存。");
    }
    setPhase("SAVING");
    operationStageRef.current = "SAVING";
    const saved = await saveTimeline(snapshot.project, timeline);
    if (runTokenRef.current !== token || !isProjectActive(saved.id)) {
      throw new Error("项目在保存后发生切换，渲染未提交。");
    }
    const finalSnapshot = await reloadProject(saved.id);
    const finalPrepared = prepareItems(prepared, finalSnapshot.project, finalSnapshot.assetVersions);
    const finalRights = await readRights(saved.id, finalPrepared, token);
    if (finalRights.some((result) => !result.check.allowed)) {
      setResume({ projectId: saved.id, items: finalPrepared });
      setPhase("BLOCKED");
      setMessage("保存后权利状态发生变化，渲染未提交。请重新治理并预检。");
      return;
    }
    if (!confirmRender) throw new Error("请确认会创建真实渲染任务。");
    const knownTaskIds = new Set((await refreshRenderTasks(saved.id)).map((task) => task.taskId));
    setPhase("SUBMITTING");
    operationStageRef.current = "SUBMITTING";
    let task: RenderTaskDTO;
    try {
      task = await submitRender(saved.id);
    } catch (submitError) {
      const refreshed = await refreshRenderTasks(saved.id).catch(() => []);
      const discovered = refreshed.find((candidate) => !knownTaskIds.has(candidate.taskId));
      if (!discovered) throw submitError;
      task = discovered;
    }
    if (runTokenRef.current !== token || !isProjectActive(saved.id)) return;
    setTaskId(task.taskId);
    setTaskStatus(task.canonicalStatus || task.status);
    setResume(null);
    setPhase("TRACKING");
    setMessage(`渲染任务 ${task.taskId} 已提交；候选成片仍需独立采纳。`);
  };

  const execute = async () => {
    if (submittingRef.current || phase !== "READY") return;
    submittingRef.current = true;
    operationStageRef.current = "VALIDATING";
    const token = ++runTokenRef.current;
    const uploadedNames: string[] = [];
    setMessage(null);
    try {
      validateDraft();
      if (!confirmRender) throw new Error("请确认会创建真实渲染任务。");
      let snapshot: QuickProjectSnapshot;
      if (projectMode === "NEW_PROJECT") {
        setPhase("CREATING_PROJECT");
        operationStageRef.current = "CREATING_PROJECT";
        snapshot = await createProject(projectName.trim());
      } else {
        snapshot = await reloadProject(currentProject!.id);
      }
      if (runTokenRef.current !== token || !isProjectActive(snapshot.project.id)) {
        throw new Error("活动项目已变化，本次执行已停止。");
      }
      let workingProject = snapshot.project;
      let workingVersions = snapshot.assetVersions;
      const materialized: DraftItem[] = [];
      for (const item of items) {
        if (item.kind === "existing") {
          materialized.push(item);
          continue;
        }
        if (!item.file) throw new Error(`文件“${item.name}”已不可用，请重新选择。`);
        setPhase("UPLOADING");
        operationStageRef.current = "UPLOADING";
        const uploaded = await uploadMedia(workingProject.id, workingProject.revision, item.file);
        workingProject = uploaded.project;
        workingVersions = [
          ...workingVersions.filter((version) => version.id !== uploaded.assetVersion.id),
          uploaded.assetVersion,
        ];
        uploadedNames.push(item.name);
        materialized.push({ ...item, mediaId: uploaded.assetVersion.mediaId });
        if (runTokenRef.current !== token || !isProjectActive(workingProject.id)) {
          throw new Error("活动项目已变化，已停止后续上传和渲染。");
        }
      }
      const prepared = prepareItems(materialized, workingProject, workingVersions);
      await arrangeSaveAndRender({ project: workingProject, assetVersions: workingVersions }, prepared, token);
    } catch (error) {
      if (runTokenRef.current !== token) return;
      const failedStage = String(operationStageRef.current);
      if (error instanceof Error && error.message.includes("并发")) setPhase("CONFLICT");
      else if (failedStage === "UPLOADING") setPhase("PARTIAL");
      else if (failedStage === "SUBMITTING") setPhase("AMBIGUOUS");
      else setPhase("FAILED");
      const failure = safeErrorMessage(error, "快速制作未完成，请检查状态后重试。");
      setMessage(uploadedNames.length > 0 && failedStage === "UPLOADING"
        ? `部分完成：已上传 ${uploadedNames.join("、")}；${failure}。已成功创建的素材版本不会被伪造回滚。`
        : failure);
    } finally {
      submittingRef.current = false;
    }
  };

  const resumeAfterGovernance = async () => {
    if (!resume || submittingRef.current) return;
    submittingRef.current = true;
    const token = ++runTokenRef.current;
    setMessage(null);
    try {
      const snapshot = await reloadProject(resume.projectId);
      if (!isProjectActive(snapshot.project.id)) throw new Error("活动项目已变化，请重新开始预检。");
      const prepared = prepareItems(resume.items, snapshot.project, snapshot.assetVersions);
      await arrangeSaveAndRender(snapshot, prepared, token);
    } catch (error) {
      if (runTokenRef.current !== token) return;
      if (error instanceof Error && error.message.includes("并发")) setPhase("CONFLICT");
      else setPhase("FAILED");
      setMessage(safeErrorMessage(error, "重新预检未完成，请检查项目状态。"));
    } finally {
      submittingRef.current = false;
    }
  };

  if (!open) {
    return (
      <section className="quick-create-entry" aria-label="快速制作入口">
        <div>
          <strong>一键短视频制作</strong>
          <span>复用现有项目、素材治理、时间线与渲染任务；不会自动采纳或发布。</span>
        </div>
        {canEdit ? (
          <button type="button" disabled={busy} onClick={() => { setOpen(true); setPhase("EDITING"); }}>
            快速制作短视频
          </button>
        ) : <span className="empty-note">当前为只读权限，仅可查看现有项目和成片。</span>}
      </section>
    );
  }

  return (
    <section className="quick-create-panel" aria-label="一键短视频制作面板">
      <div className="quick-create-heading">
        <div><strong>一键短视频制作</strong><span>状态：{quickCreatePhaseLabel(phase)}</span></div>
        <button type="button" className="secondary" disabled={working} onClick={resetDraft}>取消</button>
      </div>
      <div className="quick-create-grid">
        <fieldset disabled={working || busy}>
          <legend>1. 项目</legend>
          <label><input type="radio" name="quick-project-mode" checked={projectMode === "CURRENT_PROJECT"} disabled={!currentProject} onChange={() => setProjectMode("CURRENT_PROJECT")} /> 使用当前项目{currentProject ? `（版本 ${currentProject.revision}）` : ""}</label>
          <label><input type="radio" name="quick-project-mode" checked={projectMode === "NEW_PROJECT"} onChange={() => { setProjectMode("NEW_PROJECT"); setItems((previous) => previous.filter((item) => item.kind === "file")); setPhase("EDITING"); }} /> 新建项目</label>
          {projectMode === "NEW_PROJECT" && <input aria-label="快速制作项目名称" value={projectName} maxLength={120} placeholder="输入项目名称" onChange={(event) => setProjectName(event.target.value)} />}
          {projectMode === "CURRENT_PROJECT" && hasExistingTimeline && (
            <label className="confirmation-row"><input type="checkbox" checked={replaceExistingTimeline} onChange={(event) => setReplaceExistingTimeline(event.target.checked)} /> 确认覆盖当前项目已有时间线（版本 {currentProject?.revision}）</label>
          )}
        </fieldset>
        <fieldset disabled={working || busy}>
          <legend>2. 素材与顺序（{items.length}/{MAX_ITEMS}）</legend>
          {projectMode === "CURRENT_PROJECT" && currentProject?.materials.map((material) => (
            <label key={material.id}><input type="checkbox" checked={existingMediaIds.has(material.id)} onChange={(event) => addExisting(material, event.target.checked)} /> {material.name}</label>
          ))}
          <label className="file-picker-button">选择本地素材<input aria-label="快速制作媒体文件" className="native-file-input" type="file" multiple accept="video/*,audio/*,.mkv,.m4v" onChange={(event) => { addFiles(event.target.files); event.target.value = ""; }} /></label>
          <ol className="quick-item-list">
            {items.map((item, index) => (
              <li key={item.key}><span>{item.name} · {item.kind === "file" ? "待上传" : "既有素材"}</span><span><button type="button" className="text-button" disabled={index === 0} onClick={() => moveItem(index, -1)}>上移</button><button type="button" className="text-button" disabled={index === items.length - 1} onClick={() => moveItem(index, 1)}>下移</button><button type="button" className="text-button" onClick={() => { setItems((previous) => previous.filter((candidate) => candidate.key !== item.key)); setPhase("EDITING"); }}>移除</button></span></li>
            ))}
          </ol>
        </fieldset>
        <fieldset disabled={working || busy}>
          <legend>3. 排布与确认</legend>
          <label><input type="radio" name="quick-clip-mode" checked={clipMode === "ORIGINAL_DURATION"} onChange={() => setClipMode("ORIGINAL_DURATION")} /> 使用原时长</label>
          <label><input type="radio" name="quick-clip-mode" checked={clipMode === "FIXED_DURATION"} onChange={() => setClipMode("FIXED_DURATION")} /> 统一时长</label>
          {clipMode === "FIXED_DURATION" && <label>每段秒数<input aria-label="统一片段时长" type="number" min={1} max={30} step={1} value={fixedClipSeconds} onChange={(event) => setFixedClipSeconds(Number(event.target.value))} /></label>}
          <label><input type="checkbox" checked readOnly /> 强制导出权利检查（不可关闭）</label>
          <label className="confirmation-row"><input type="checkbox" checked={confirmRender} onChange={(event) => setConfirmRender(event.target.checked)} /> 确认通过后会创建真实渲染任务，不会自动采纳或发布</label>
        </fieldset>
      </div>
      {rights.length > 0 && (
        <div className="quick-rights" aria-label="快速制作权利预检结果">
          {rights.map((result) => <div key={result.assetVersionId}><span>{result.name} · v{result.versionNo} · {result.sha256.slice(0, 10)}…</span><span className={`status-pill ${result.check.allowed ? "success" : "warning"}`}>{rightsDecisionLabel(result.check.code)}</span></div>)}
        </div>
      )}
      {message && <div className={phase === "TRACKING" ? "inline-success" : "rights-failures"} role={phase === "TRACKING" ? "status" : "alert"}>{message}</div>}
      <div className="quick-create-actions">
        <button type="button" className="secondary" disabled={working || busy} onClick={preflight}>执行预检</button>
        <button type="button" disabled={phase !== "READY" || !confirmRender || busy} onClick={execute}>一键生成短视频</button>
        {(resume || (phase === "BLOCKED" && rights.some((result) => !result.check.allowed))) && <button type="button" className="secondary" onClick={onOpenGovernance}>前往素材治理</button>}
        {resume && <button type="button" onClick={resumeAfterGovernance}>重新预检并继续</button>}
        {taskId && <button type="button" className="secondary" onClick={onViewTask}>查看任务</button>}
        {taskId && taskStatus === "SUCCEEDED" && <button type="button" className="secondary" onClick={onViewFinished}>查看候选成片</button>}
      </div>
      <p className="quick-create-boundary">本功能只编排现有能力；不调用真实插件/模型，不自动采纳母版，不部署或公开发布。</p>
    </section>
  );
}
