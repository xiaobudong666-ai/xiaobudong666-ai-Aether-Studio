import React, { useState, useEffect } from "react";
import { ClipDTO } from "@aether/contracts";
import {
  safeErrorMessage,
  taskMessageLabel,
  taskStatusLabel,
} from "../i18n";

interface PropertyInspectorProps {
  selectedClip: ClipDTO | null;
  projectId: string | null;
  onTriggerRender: () => Promise<void>;
  apiBase: string;
  canRender: boolean;
}

export interface RenderTask {
  taskId: string;
  projectId: string;
  progress: number;
  status: string;
  message: string;
  artifactUrl?: string;
  attempts?: number;
  createdAt?: string;
}

export const PropertyInspector: React.FC<PropertyInspectorProps> = ({
  selectedClip,
  projectId,
  onTriggerRender,
  apiBase,
  canRender,
}) => {
  const [tasks, setTasks] = useState<RenderTask[]>([]);
  const [sseConnected, setSseConnected] = useState(false);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!projectId) {
      setTasks([]);
      setTaskError(null);
      return;
    }
    let cancelled = false;
    fetch(`${apiBase}/render-tasks?projectId=${encodeURIComponent(projectId)}`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("任务历史加载失败")))
      .then((payload: RenderTask[]) => {
        if (!cancelled) {
          setTasks(payload);
          setTaskError(null);
        }
      })
      .catch(() => {
        if (!cancelled) setTaskError("任务历史暂时无法加载，请稍后刷新页面。");
      });
    return () => { cancelled = true; };
  }, [apiBase, projectId]);

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
        const payload = JSON.parse(e.data) as RenderTask;
        if (projectId && payload.projectId !== projectId) return;
        setTasks((prev) => {
          // If task exists, update it. Otherwise insert
          const idx = prev.findIndex((t) => t.taskId === payload.taskId);
          if (idx > -1) {
            const next = [...prev];
            next[idx] = payload;
            return next;
          } else {
            return [payload, ...prev];
          }
        });
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
    setSubmitting(true);
    setTaskError(null);
    try {
      await onTriggerRender();
    } catch (err) {
      setTaskError(safeErrorMessage(err, "渲染任务提交失败，请稍后重试。"));
    } finally {
      setSubmitting(false);
    }
  };

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
          <div style={{ fontWeight: 600, fontSize: "12px", color: "#9ca3af", marginBottom: "8px" }}>持久任务记录 · 实时更新</div>
          <div style={{ flex: 1, overflowY: "auto" }}>
            {tasks.length === 0 ? (
              <div style={{ color: "#a1a1aa", fontStyle: "italic", fontSize: "12px" }}>还没有提交后台任务。</div>
            ) : (
              tasks.map((t) => (
                <div key={t.taskId} className={`task-card ${t.status === "completed" ? "completed" : t.status === "failed" ? "failed" : ""}`}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontWeight: 600, fontSize: "12px" }}>
                    <span style={{ color: "#a78bfa" }}>任务：{t.taskId.substring(0, 8)}</span>
                    <span>{taskStatusLabel(t.status)}</span>
                  </div>
                  <div style={{ fontSize: "12px", color: "#d4d4d8", marginTop: "4px" }}>{taskMessageLabel(t.status)}</div>
                  {typeof t.attempts === "number" && t.attempts > 1 && (
                    <div style={{ fontSize: "12px", color: "#fbbf24", marginTop: "4px" }}>已自动重试 {t.attempts - 1} 次</div>
                  )}
                  {t.artifactUrl && (
                    <a href={t.artifactUrl} download style={{ fontSize: "12px", color: "#93c5fd" }}>
                      下载 MP4 成片
                    </a>
                  )}
                  <div style={{ display: "flex", alignItems: "center", gap: "8px", marginTop: "4px" }}>
                    <div className="progress-bar-container" style={{ flex: 1 }}>
                      <div className="progress-bar" style={{ width: `${t.progress}%`, backgroundColor: t.status === "completed" ? "#10b981" : t.status === "failed" ? "#ef4444" : "#3b82f6" }} />
                    </div>
                    <span style={{ fontSize: "12px", color: "#a1a1aa" }}>{t.progress}%</span>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

      </div>
    </div>
  );
};
