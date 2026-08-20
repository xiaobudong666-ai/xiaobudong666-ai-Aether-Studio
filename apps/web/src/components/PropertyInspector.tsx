import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  CanonicalTaskStatusDTO,
  ClipDTO,
  RenderTaskDTO,
  RenderTaskSchema,
  canonicalTaskStatus,
} from "@aether/contracts";
import {
  safeErrorMessage,
  taskMessageLabel,
  taskStatusLabel,
} from "../i18n";
import { FinishedMediaPanel } from "./FinishedMediaPanel";

interface PropertyInspectorProps {
  selectedClip: ClipDTO | null;
  projectId: string | null;
  onTriggerRender: () => Promise<void>;
  apiBase: string;
  canEdit: boolean;
  canRender: boolean;
  onSessionExpired: () => void;
}

function resolvedTaskStatus(task: RenderTaskDTO): CanonicalTaskStatusDTO {
  if (task.canonicalStatus) return task.canonicalStatus;
  try {
    return canonicalTaskStatus(task.status);
  } catch {
    return "UNKNOWN";
  }
}

function taskTimestamp(task: RenderTaskDTO): number {
  const value = Date.parse(task.updatedAt || task.createdAt || "");
  return Number.isNaN(value) ? 0 : value;
}

function taskDateLabel(task: RenderTaskDTO): string | null {
  const value = task.updatedAt || task.createdAt;
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(date);
}

function mergeTasks(
  current: RenderTaskDTO[],
  incoming: RenderTaskDTO[],
): RenderTaskDTO[] {
  const byId = new Map(current.map((task) => [task.taskId, task]));
  incoming.forEach((task) => {
    const existing = byId.get(task.taskId);
    if (!existing || taskTimestamp(task) >= taskTimestamp(existing)) {
      byId.set(task.taskId, task);
    }
  });
  return [...byId.values()].sort((left, right) => taskTimestamp(right) - taskTimestamp(left));
}

export const PropertyInspector: React.FC<PropertyInspectorProps> = ({
  selectedClip,
  projectId,
  onTriggerRender,
  apiBase,
  canEdit,
  canRender,
  onSessionExpired,
}) => {
  const [tasks, setTasks] = useState<RenderTaskDTO[]>([]);
  const [sseConnected, setSseConnected] = useState(false);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const projectIdRef = useRef(projectId);
  const loadRequestIdRef = useRef(0);
  projectIdRef.current = projectId;

  const loadTasks = useCallback(async () => {
    const activeProjectId = projectId;
    const requestId = ++loadRequestIdRef.current;
    const isCurrentRequest = () => (
      loadRequestIdRef.current === requestId
      && projectIdRef.current === activeProjectId
    );
    if (!activeProjectId) {
      setTasks([]);
      setTaskError(null);
      return;
    }
    try {
      const response = await fetch(`${apiBase}/render-tasks?projectId=${encodeURIComponent(activeProjectId)}`);
      if (!isCurrentRequest()) return;
      if (response.status === 401) {
        onSessionExpired();
        return;
      }
      if (!response.ok) throw new Error("任务历史加载失败");
      const payload = await response.json();
      if (!isCurrentRequest()) return;
      if (!Array.isArray(payload)) throw new Error("任务历史格式异常");
      const parsed = payload.map((task) => RenderTaskSchema.parse(task));
      setTasks(mergeTasks([], parsed));
      setTaskError(null);
    } catch {
      if (isCurrentRequest()) {
        setTaskError("任务历史暂时无法加载；已有数据已保留，可以手动刷新。");
      }
    }
  }, [apiBase, onSessionExpired, projectId]);

  useEffect(() => {
    setTasks([]);
    setTaskError(null);
    setSubmitting(false);
  }, [projectId]);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  useEffect(() => {
    // Dynamically build URL from apiBase prop
    const sseUrl = `${apiBase}/events`;
    const eventSource = new EventSource(sseUrl);

    eventSource.onopen = () => {
      setSseConnected(true);
    };

    eventSource.onerror = () => {
      setSseConnected(false);
    };

    // Listen to task progress updates
    eventSource.addEventListener("task_progress", (e: any) => {
      try {
        const parsed = RenderTaskSchema.safeParse(JSON.parse(e.data));
        if (!parsed.success || parsed.data.projectId !== projectId) return;
        setTasks((previous) => mergeTasks(previous, [parsed.data]));
      } catch (err) {
        console.error("实时任务数据解析失败", err);
      }
    });

    return () => {
      eventSource.close();
    };
  }, [apiBase, projectId]);

  const handleRender = async () => {
    if (!projectId) return;
    const activeProjectId = projectId;
    setSubmitting(true);
    setTaskError(null);
    try {
      await onTriggerRender();
    } catch (err) {
      if (projectIdRef.current === activeProjectId) {
        setTaskError(safeErrorMessage(err, "渲染任务提交失败，请稍后重试。"));
      }
    } finally {
      if (projectIdRef.current === activeProjectId) setSubmitting(false);
    }
  };

  const projectTasks = tasks.filter((task) => task.projectId === projectId);

  return (
    <div className="panel" style={{ height: "100%", borderRight: "none" }}>
      <div className="panel-header">属性与任务</div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: "16px" }}>

        {/* Connection status */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: "12px", color: sseConnected ? "#34d399" : "#f87171" }}>
          <span>实时任务连接</span>
          <span aria-live="polite">● {sseConnected ? "已连接" : "未连接"}</span>
        </div>

        {/* Selected Clip Detail */}
        <div style={{ background: "#1e1e24", padding: "10px", borderRadius: "6px", border: "1px solid #2d2d34" }}>
          <div style={{ fontWeight: 600, fontSize: "12px", color: "#a78bfa", marginBottom: "8px" }}>已选片段</div>
          {selectedClip ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "6px", fontSize: "12px" }}>
              <div><strong>片段编号：</strong> <span style={{ fontFamily: "monospace" }}>{selectedClip.id}</span></div>
              <div><strong>轨道编号：</strong> {selectedClip.trackId}</div>
              <div><strong>素材编号：</strong> {selectedClip.materialId}</div>
              <div>
                <strong>时间线起点：</strong> {selectedClip.start.value}/{selectedClip.start.timescale}（{ (selectedClip.start.value / selectedClip.start.timescale).toFixed(2) } 秒）
              </div>
              <div>
                <strong>片段时长：</strong> {selectedClip.duration.value}/{selectedClip.duration.timescale}（{ (selectedClip.duration.value / selectedClip.duration.timescale).toFixed(2) } 秒）
              </div>
            </div>
          ) : (
            <div style={{ color: "#a1a1aa", fontStyle: "italic", fontSize: "12px" }}>尚未选择片段。点击下方时间线中的片段可查看详情。</div>
          )}
        </div>

        {/* Rendering Launcher */}
        <div style={{ background: "#1e1e24", padding: "10px", borderRadius: "6px", border: "1px solid #2d2d34", display: "flex", flexDirection: "column", gap: "8px" }}>
          <div style={{ fontWeight: 600, fontSize: "12px", color: "#f59e0b" }}>成片渲染</div>
          <button onClick={handleRender} disabled={!projectId || !canRender || submitting} style={{ background: "#f59e0b" }}>
            {submitting ? "正在提交…" : "提交渲染任务"}
          </button>
          <div style={{ fontSize: "12px", color: "#a1a1aa" }}>
            使用固定版本的 video-use 与真实 FFmpeg。请先上传视频并添加到时间线。
          </div>
          {taskError && <div className="inline-error" role="alert">{taskError}</div>}
        </div>

        {/* Task Tracker list */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: "150px" }}>
          <div className="section-title-row">
            <strong>持久任务记录 · 规范状态</strong>
            <button type="button" className="text-button" onClick={() => void loadTasks()} disabled={!projectId}>
              刷新
            </button>
          </div>
          <div style={{ flex: 1, overflowY: "auto" }}>
            {projectTasks.length === 0 ? (
              <div style={{ color: "#a1a1aa", fontStyle: "italic", fontSize: "12px" }}>还没有提交后台任务。</div>
            ) : (
              projectTasks.map((t) => {
                const canonicalStatus = resolvedTaskStatus(t);
                const updatedLabel = taskDateLabel(t);
                const statusClass = canonicalStatus === "SUCCEEDED"
                  ? "completed"
                  : canonicalStatus === "FAILED"
                    ? "failed"
                    : canonicalStatus === "PARTIAL" || canonicalStatus === "UNKNOWN"
                      ? "warning"
                      : canonicalStatus === "CANCELED"
                        ? "muted"
                        : "";
                const progressColor = canonicalStatus === "SUCCEEDED"
                  ? "#10b981"
                  : canonicalStatus === "FAILED"
                    ? "#ef4444"
                    : canonicalStatus === "PARTIAL" || canonicalStatus === "UNKNOWN"
                      ? "#f59e0b"
                      : "#3b82f6";
                return (
                  <div key={t.taskId} className={`task-card ${statusClass}`}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontWeight: 600, fontSize: "12px" }}>
                      <span style={{ color: "#a78bfa" }}>任务：{t.taskId.substring(0, 8)}</span>
                      <span>{taskStatusLabel(canonicalStatus)}</span>
                    </div>
                    <div className="task-server-message">{t.message || taskMessageLabel(canonicalStatus)}</div>
                    <div className="task-guidance">{taskMessageLabel(canonicalStatus)}</div>
                    {updatedLabel && <div className="task-meta">更新时间：{updatedLabel}</div>}
                    {typeof t.attempts === "number" && t.attempts > 1 && (
                      <div style={{ fontSize: "12px", color: "#fbbf24", marginTop: "4px" }}>已自动重试 {t.attempts - 1} 次</div>
                    )}
                    {canonicalStatus === "UNKNOWN" && (
                      <button type="button" className="secondary" onClick={() => void loadTasks()}>
                        重新查询状态
                      </button>
                    )}
                    {canonicalStatus === "SUCCEEDED" && t.artifactUrl && (
                      <a href={t.artifactUrl} download style={{ fontSize: "12px", color: "#93c5fd" }}>
                        下载 MP4 成片
                      </a>
                    )}
                    <div style={{ display: "flex", alignItems: "center", gap: "8px", marginTop: "4px" }}>
                      <div className="progress-bar-container" style={{ flex: 1 }}>
                        <div className="progress-bar" style={{ width: `${t.progress}%`, backgroundColor: progressColor }} />
                      </div>
                      <span style={{ fontSize: "12px", color: "#a1a1aa" }}>{t.progress}%</span>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        <FinishedMediaPanel
          apiBase={apiBase}
          projectId={projectId}
          canEdit={canEdit}
          onSessionExpired={onSessionExpired}
          refreshToken={projectTasks
            .filter((task) => resolvedTaskStatus(task) === "SUCCEEDED")
            .map((task) => `${task.taskId}:${task.updatedAt || task.createdAt || ""}`)
            .join("|")}
        />

      </div>
    </div>
  );
};
