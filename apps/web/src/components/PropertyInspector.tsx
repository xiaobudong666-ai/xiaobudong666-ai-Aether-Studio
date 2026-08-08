import React, { useState, useEffect } from "react";
import { ClipDTO } from "@aether/contracts";

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

  useEffect(() => {
    if (!projectId) {
      setTasks([]);
      return;
    }
    let cancelled = false;
    fetch(`${apiBase}/render-tasks?projectId=${encodeURIComponent(projectId)}`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("Task history unavailable")))
      .then((payload: RenderTask[]) => {
        if (!cancelled) setTasks(payload);
      })
      .catch((error) => console.error(error));
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
        console.error("Failed to parse SSE task progress data", err);
      }
    });

    return () => {
      eventSource.close();
    };
  }, [apiBase, projectId]);

  const handleRender = async () => {
    if (!projectId) return;
    try {
      await onTriggerRender();
    } catch (err) {
      alert("Error triggering render: " + err);
    }
  };

  return (
    <div className="panel" style={{ height: "100%", borderRight: "none" }}>
      <div className="panel-header">Property Inspector & Tasks</div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: "16px" }}>

        {/* Connection status */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: "11px", color: sseConnected ? "#10b981" : "#ef4444" }}>
          <span>SSE Service Link:</span>
          <span>● {sseConnected ? "Connected" : "Disconnected"}</span>
        </div>

        {/* Selected Clip Detail */}
        <div style={{ background: "#1e1e24", padding: "10px", borderRadius: "6px", border: "1px solid #2d2d34" }}>
          <div style={{ fontWeight: 600, fontSize: "12px", color: "#a78bfa", marginBottom: "8px" }}>Selected Clip Info</div>
          {selectedClip ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "6px", fontSize: "12px" }}>
              <div><strong>ID:</strong> <span style={{ fontFamily: "monospace" }}>{selectedClip.id}</span></div>
              <div><strong>Track ID:</strong> {selectedClip.trackId}</div>
              <div><strong>Material ID:</strong> {selectedClip.materialId}</div>
              <div>
                <strong>Timeline Start:</strong> {selectedClip.start.value}/{selectedClip.start.timescale} ({ (selectedClip.start.value / selectedClip.start.timescale).toFixed(2) }s)
              </div>
              <div>
                <strong>Clip Duration:</strong> {selectedClip.duration.value}/{selectedClip.duration.timescale} ({ (selectedClip.duration.value / selectedClip.duration.timescale).toFixed(2) }s)
              </div>
            </div>
          ) : (
            <div style={{ color: "#71717a", fontStyle: "italic", fontSize: "12px" }}>No clip selected on timeline. Click a clip below to view.</div>
          )}
        </div>

        {/* Rendering Launcher */}
        <div style={{ background: "#1e1e24", padding: "10px", borderRadius: "6px", border: "1px solid #2d2d34", display: "flex", flexDirection: "column", gap: "8px" }}>
          <div style={{ fontWeight: 600, fontSize: "12px", color: "#f59e0b" }}>Render Actions</div>
          <button onClick={handleRender} disabled={!projectId || !canRender} style={{ background: "#f59e0b" }}>
            🚀 Render with video-use
          </button>
          <div style={{ fontSize: "11px", color: "#71717a" }}>
            Uses the pinned video-use pipeline and real FFmpeg. Upload and place a video first.
          </div>
        </div>

        {/* Task Tracker list */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: "150px" }}>
          <div style={{ fontWeight: 600, fontSize: "12px", color: "#9ca3af", marginBottom: "8px" }}>Persistent Task History + Live SSE</div>
          <div style={{ flex: 1, overflowY: "auto" }}>
            {tasks.length === 0 ? (
              <div style={{ color: "#71717a", fontStyle: "italic", fontSize: "12px" }}>No background tasks triggered yet.</div>
            ) : (
              tasks.map((t) => (
                <div key={t.taskId} className={`task-card ${t.status === "completed" ? "completed" : t.status === "failed" ? "failed" : ""}`}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontWeight: 600, fontSize: "11px" }}>
                    <span style={{ color: "#a78bfa" }}>Task: {t.taskId.substring(0, 8)}</span>
                    <span style={{ textTransform: "capitalize" }}>{t.status}</span>
                  </div>
                  <div style={{ fontSize: "11px", color: "#d4d4d8", marginTop: "4px" }}>{t.message}</div>
                  {t.artifactUrl && (
                    <a href={t.artifactUrl} style={{ fontSize: "11px", color: "#60a5fa" }}>
                      Download MP4
                    </a>
                  )}
                  <div style={{ display: "flex", alignItems: "center", gap: "8px", marginTop: "4px" }}>
                    <div className="progress-bar-container" style={{ flex: 1 }}>
                      <div className="progress-bar" style={{ width: `${t.progress}%`, backgroundColor: t.status === "completed" ? "#10b981" : t.status === "failed" ? "#ef4444" : "#3b82f6" }} />
                    </div>
                    <span style={{ fontSize: "10px", color: "#a1a1aa" }}>{t.progress}%</span>
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
