import { useEffect, useRef, useState } from "react";
import { RationalTime, ProjectDTO, ClipDTO } from "@aether/contracts";
import { AssetLibrary } from "./components/AssetLibrary";
import { CanvasPreview } from "./components/CanvasPreview";
import { PropertyInspector } from "./components/PropertyInspector";
import { Timeline } from "./components/Timeline";
import {
  apiErrorMessage,
  formatBytes,
  roleLabel,
  safeErrorMessage,
} from "./i18n";

interface AuthUser {
  id: string;
  email: string;
  displayName: string;
  role: "owner" | "editor" | "viewer";
  tenant: { id: string; name: string; slug: string };
  quotas: {
    projects: number;
    storageBytes: number;
    storageBytesUsed: number;
    concurrentRenders: number;
    monthlyRenderSeconds: number;
    monthlyRenderSecondsUsed: number;
    period: string;
  };
}

export default function App() {
  const [projects, setProjects] = useState<ProjectDTO[]>([]);
  const [currentProject, setCurrentProject] = useState<ProjectDTO | null>(null);
  const [newProjectName, setNewProjectName] = useState("");
  const [selectedClip, setSelectedClip] = useState<ClipDTO | null>(null);
  const [currentTime, setCurrentTime] = useState<RationalTime>(new RationalTime(0, 24000));
  const [apiError, setApiError] = useState<string | null>(null);
  const [authUser, setAuthUser] = useState<AuthUser | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [loginEmail, setLoginEmail] = useState("admin@aether.local");
  const [loginPassword, setLoginPassword] = useState("");
  const [loginError, setLoginError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [isLoggingIn, setIsLoggingIn] = useState(false);
  const [isCreatingProject, setIsCreatingProject] = useState(false);
  const [isSavingProject, setIsSavingProject] = useState(false);
  const savingProjectRef = useRef(false);

  // Production uses the same-origin Nginx /api proxy. Local Vite mirrors it.
  const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";
  const OPENREEL_URL = (import.meta.env.VITE_OPENREEL_URL || "").trim();
  const canEdit = authUser?.role === "owner" || authUser?.role === "editor";

  const stateHeaders = { "X-Aether-CSRF": "1" };

  const handleExpiredSession = (response: Response): boolean => {
    if (response.status !== 401) return false;
    setAuthUser(null);
    setProjects([]);
    setCurrentProject(null);
    setLoginError("登录已过期，请重新登录。");
    return true;
  };

  const loadIdentity = async () => {
    try {
      const response = await fetch(`${API_BASE}/auth/me`);
      if (!response.ok) {
        setAuthUser(null);
        return;
      }
      setAuthUser(await response.json());
    } catch {
      setAuthUser(null);
      setLoginError("登录服务暂时不可用，请稍后重试。");
    } finally {
      setAuthLoading(false);
    }
  };

  // 1. Fetch projects on load
  const fetchProjects = async () => {
    try {
      const res = await fetch(`${API_BASE}/projects`);
      if (handleExpiredSession(res)) return;
      if (res.ok) {
        const data = await res.json();
        setProjects(data);
        if (data.length > 0 && !currentProject) {
          // Default to first project
          fetchProjectDetail(data[0].id);
        }
        setApiError(null);
      } else {
        const payload = await res.json().catch(() => null);
        throw new Error(apiErrorMessage(payload, "项目列表加载失败。"));
      }
    } catch (err) {
      setApiError(safeErrorMessage(err, "服务暂时不可用，请稍后重试。"));
    }
  };

  useEffect(() => {
    loadIdentity();
    // Identity is intentionally checked once when the SPA starts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (authUser) fetchProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authUser?.id]);

  const handleLogin = async (event: React.FormEvent) => {
    event.preventDefault();
    if (isLoggingIn) return;
    setIsLoggingIn(true);
    setLoginError(null);
    try {
      const response = await fetch(`${API_BASE}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: loginEmail, password: loginPassword }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        setLoginError(apiErrorMessage(payload, "登录失败，请检查邮箱和密码。"));
        return;
      }
      setAuthUser(await response.json());
      setLoginPassword("");
      setActionMessage("登录成功，正在载入工作区。");
    } catch {
      setLoginError("登录服务暂时不可用，请稍后重试。");
    } finally {
      setIsLoggingIn(false);
    }
  };

  const handleLogout = async () => {
    try {
      await fetch(`${API_BASE}/auth/logout`, { method: "POST", headers: stateHeaders });
    } finally {
      setActionMessage(null);
    }
    setAuthUser(null);
    setProjects([]);
    setCurrentProject(null);
  };

  const fetchProjectDetail = async (id: string) => {
    try {
      const res = await fetch(`${API_BASE}/projects/${id}`);
      if (handleExpiredSession(res)) return;
      if (res.ok) {
        const data = await res.json();
        setCurrentProject(data);
        setSelectedClip(null);
        setCurrentTime(new RationalTime(0, 24000));
        setApiError(null);
      } else {
        const payload = await res.json().catch(() => null);
        setApiError(apiErrorMessage(payload, "项目详情加载失败。"));
      }
    } catch (err) {
      setApiError(safeErrorMessage(err, "项目详情加载失败。"));
    }
  };

  // 2. Create a new project
  const handleCreateProject = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProjectName.trim() || isCreatingProject) return;
    setIsCreatingProject(true);

    try {
      const res = await fetch(`${API_BASE}/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...stateHeaders },
        body: JSON.stringify({ name: newProjectName.trim() }),
      });
      if (handleExpiredSession(res)) return;
      if (res.ok) {
        const newProj = await res.json();
        setProjects((prev) => [...prev, newProj]);
        setCurrentProject(newProj);
        setNewProjectName("");
        setSelectedClip(null);
        setActionMessage(`项目“${newProj.name}”已创建。`);
      } else {
        const payload = await res.json().catch(() => null);
        setApiError(apiErrorMessage(payload, "项目创建失败。"));
      }
    } catch (err) {
      setApiError(safeErrorMessage(err, "项目创建失败。"));
    } finally {
      setIsCreatingProject(false);
    }
  };

  // 3. Save current project (Update) with optimistic lock checks
  const saveProjectState = async (updatedProj: ProjectDTO): Promise<boolean> => {
    if (savingProjectRef.current) return false;
    savingProjectRef.current = true;
    const previousProject = currentProject;
    setIsSavingProject(true);
    // Optimistically update locally
    setCurrentProject(updatedProj);
    setProjects((prev) => prev.map((p) => (p.id === updatedProj.id ? updatedProj : p)));

    try {
      const res = await fetch(`${API_BASE}/projects/${updatedProj.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...stateHeaders },
        body: JSON.stringify({
          name: updatedProj.name,
          timeline: updatedProj.timeline,
          expectedRevision: updatedProj.revision - 1, // Prior revision
        }),
      });
      if (handleExpiredSession(res)) return false;
      if (res.status === 409) {
        const payload = await res.json().catch(() => null);
        setApiError(apiErrorMessage(payload, "项目已在其他页面更新，正在载入最新版本。"));
        await fetchProjectDetail(updatedProj.id);
        return false;
      } else if (res.ok) {
        const latest = await res.json();
        setCurrentProject(latest);
        setProjects((prev) => prev.map((project) => (
          project.id === latest.id ? latest : project
        )));
        setApiError(null);
        return true;
      } else {
        const payload = await res.json().catch(() => null);
        throw new Error(apiErrorMessage(payload, "项目保存失败。"));
      }
    } catch (err) {
      if (previousProject) {
        setCurrentProject(previousProject);
        setProjects((prev) => prev.map((project) => (
          project.id === previousProject.id ? previousProject : project
        )));
      }
      setApiError(safeErrorMessage(err, "项目保存失败。"));
      throw err;
    } finally {
      savingProjectRef.current = false;
      setIsSavingProject(false);
    }
  };

  // 4. Upload and probe real media through the isolated video-use service.
  const handleUploadMaterial = async (file: File) => {
    if (!currentProject) throw new Error("请先创建或选择一个项目。");
    const data = new FormData();
    data.append("expectedRevision", String(currentProject.revision));
    data.append("file", file);
    const response = await fetch(`${API_BASE}/projects/${currentProject.id}/media`, {
      method: "POST",
      headers: stateHeaders,
      body: data,
    });
    if (handleExpiredSession(response)) throw new Error("登录已过期，请重新登录。");
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new Error(apiErrorMessage(payload, "媒体上传失败。"));
    }
    const payload = await response.json();
    const updatedProject = payload.project as ProjectDTO;
    setCurrentProject(updatedProject);
    setProjects((prev) => prev.map((project) => (
      project.id === updatedProject.id ? updatedProject : project
    )));
    setActionMessage(`素材“${file.name}”已上传并完成媒体信息检测。`);
  };

  // 5. Place material on timeline as a clip
  const handleAddClipToTimeline = async (materialId: string) => {
    if (!currentProject) return;
    const material = currentProject.materials.find((m) => m.id === materialId);
    if (!material) return;

    // Determine duration: fallback to 5 seconds if not defined
    const clipDuration = material.duration || { value: 120000, timescale: 24000 };

    // Find if track of material type exists, otherwise create it
    const trackType = material.type === "audio" ? "audio" : "video";
    const tracks = currentProject.timeline.tracks.map((track) => ({
      ...track,
      clips: [...track.clips],
    }));
    let targetTrack = tracks.find((t) => t.type === trackType);

    if (!targetTrack) {
      targetTrack = {
        id: `track-${Math.random().toString(36).substr(2, 9)}`,
        name: material.type === "audio" ? "音频轨道 1" : "视频轨道 1",
        type: trackType,
        clips: [],
      };
      tracks.push(targetTrack);
    }

    // Append clip to the target track using rational time offsets
    let clipStartOffset = new RationalTime(0, clipDuration.timescale);
    targetTrack.clips.forEach((clip) => {
      const clipEnd = new RationalTime(
        clip.start.value,
        clip.start.timescale,
      ).add(new RationalTime(clip.duration.value, clip.duration.timescale));
      if (clipEnd.greaterThan(clipStartOffset)) {
        clipStartOffset = clipEnd;
      }
    });

    const newClip: ClipDTO = {
      id: `clip-${Math.random().toString(36).substr(2, 9)}`,
      trackId: targetTrack.id,
      materialId: material.id,
      start: clipStartOffset.toJSON(),
      duration: clipDuration,
      sourceIn: { value: 0, timescale: clipDuration.timescale },
      volume: 1,
      opacity: 1,
      x: 0,
      y: 0,
    };

    targetTrack.clips.push(newClip);

    const updatedProj: ProjectDTO = {
      ...currentProject,
      timeline: {
        ...currentProject.timeline,
        tracks: tracks,
      },
      revision: currentProject.revision + 1,
      updatedAt: new Date().toISOString(),
    };

    const saved = await saveProjectState(updatedProj);
    if (!saved) return;
    setSelectedClip(newClip);
    setActionMessage(`素材“${material.name}”已添加到时间线。`);
  };

  // 6. Trigger backend render task
  const handleTriggerRender = async () => {
    if (!currentProject) return;
    const res = await fetch(`${API_BASE}/projects/${currentProject.id}/render`, {
      method: "POST",
      headers: stateHeaders,
    });
    if (handleExpiredSession(res)) throw new Error("登录已过期，请重新登录。");
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      throw new Error(apiErrorMessage(payload, "渲染任务提交失败。"));
    }
    setActionMessage("渲染任务已提交，可以离开页面后再回来查看进度。");
  };

  const handleExportOpenCutSnapshot = async () => {
    if (!currentProject) return;
    const { createOpenCutCompatibilitySnapshot } = await import("@aether/editor");
    const snapshot = createOpenCutCompatibilitySnapshot(currentProject);
    const blob = new Blob([JSON.stringify(snapshot, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${currentProject.name.replace(/[^a-zA-Z0-9_-]+/g, "-") || "aether-project"}.opencut.json`;
    link.click();
    URL.revokeObjectURL(url);
    setActionMessage("OpenCut 兼容快照已导出。");
  };

  const handleExportOpenReelProject = async () => {
    if (!currentProject) return;
    const { createOpenReelProjectFile } = await import("@aether/editor");
    const projectFile = createOpenReelProjectFile(currentProject);
    const blob = new Blob([JSON.stringify(projectFile, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${currentProject.name.replace(/[^a-zA-Z0-9_-]+/g, "-") || "aether-project"}.openreel.json`;
    link.click();
    URL.revokeObjectURL(url);
    setActionMessage("OpenReel 项目文件已导出。");
  };

  // Calculate timeline total duration
  const getTimelineDuration = (): RationalTime => {
    if (!currentProject) return new RationalTime(0, 24000);
    let maximum = new RationalTime(0, 24000);
    currentProject.timeline.tracks.forEach((track) => {
      track.clips.forEach((clip) => {
        const end = new RationalTime(
          clip.start.value,
          clip.start.timescale,
        ).add(new RationalTime(clip.duration.value, clip.duration.timescale));
        if (end.greaterThan(maximum)) {
          maximum = end;
        }
      });
    });
    return maximum;
  };

  const timelineDuration = getTimelineDuration();
  const previewMaterial = currentProject?.materials.find(
    (material) => material.id === selectedClip?.materialId,
  ) || currentProject?.materials.find((material) => material.type === "video") || null;

  if (authLoading) {
    return <div className="auth-screen"><div className="auth-card">正在加载 Aether Studio…</div></div>;
  }

  if (!authUser) {
    return (
      <div className="auth-screen">
        <form className="auth-card" onSubmit={handleLogin}>
          <h1>Aether Studio</h1>
          <p>登录你的安全漫剧工作区</p>
          <label>邮箱<input aria-label="邮箱" type="email" value={loginEmail} onChange={(event) => setLoginEmail(event.target.value)} required /></label>
          <label>密码<input aria-label="密码" type="password" value={loginPassword} onChange={(event) => setLoginPassword(event.target.value)} required /></label>
          {loginError && <div className="auth-error" role="alert">{loginError}</div>}
          <button type="submit" disabled={isLoggingIn}>
            {isLoggingIn ? "正在登录…" : "登录"}
          </button>
        </form>
      </div>
    );
  }

  return (
    <div className="app-shell">
      {/* Header */}
      <header className="editor-header">
        <div className="editor-logo">
          Aether Studio · AI 漫剧工作台
          <span style={{ fontSize: "12px", color: "#a1a1aa", marginLeft: "8px" }}>
            OpenCut 内核 0.2.10
          </span>
        </div>
        <div className="project-select-container">
          <span className="tenant-badge" title={`${roleLabel(authUser.role)} · ${authUser.email}`}>
            {authUser.tenant.name} · {roleLabel(authUser.role)}
          </span>
          <span className="quota-badge" title="当前团队资源使用情况">
            项目 {projects.length}/{authUser.quotas.projects} · 存储 {formatBytes(authUser.quotas.storageBytesUsed)}/{formatBytes(authUser.quotas.storageBytes)} · 本月渲染 {authUser.quotas.monthlyRenderSecondsUsed}/{authUser.quotas.monthlyRenderSeconds} 秒
          </span>
          {apiError && <span style={{ fontSize: "12px", color: "#f59e0b" }}>{apiError}</span>}
          <form onSubmit={handleCreateProject} style={{ display: "flex", gap: "6px" }}>
            <input
              type="text"
              aria-label="新项目名称"
              placeholder="输入新项目名称"
              value={newProjectName}
              onChange={(e) => setNewProjectName(e.target.value)}
              disabled={!canEdit}
            />
            <button
              type="submit"
              disabled={!canEdit || isCreatingProject || !newProjectName.trim()}
            >
              {isCreatingProject ? "正在创建…" : "创建项目"}
            </button>
          </form>
          <select
            aria-label="选择项目"
            value={currentProject?.id || ""}
            onChange={(e) => fetchProjectDetail(e.target.value)}
            disabled={projects.length === 0}
          >
            {projects.length === 0 && <option value="">暂无项目</option>}
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}（版本 {p.revision}）
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={!currentProject}
            onClick={handleExportOpenCutSnapshot}
            title="导出固定版本的 OpenCut Classic 兼容快照和素材清单"
          >
            导出 OpenCut 快照
          </button>
          <button
            type="button"
            disabled={!currentProject}
            onClick={handleExportOpenReelProject}
            title="导出 OpenReel 1.0.0 项目文件，素材可在外部重新关联"
          >
            导出 OpenReel 项目
          </button>
          {OPENREEL_URL && (
            <a href={OPENREEL_URL} target="_blank" rel="noreferrer noopener">
              打开 OpenReel
            </a>
          )}
          <button type="button" className="secondary" onClick={handleLogout}>退出登录</button>
        </div>
      </header>

      {actionMessage && (
        <div className="status-banner" role="status" aria-live="polite">
          {actionMessage}
          <button type="button" aria-label="关闭提示" onClick={() => setActionMessage(null)}>关闭</button>
        </div>
      )}

      {/* Main workbench */}
      <main className="workbench-container">
        <AssetLibrary
          materials={currentProject?.materials || []}
          onUploadMaterial={handleUploadMaterial}
          onAddClipToTimeline={handleAddClipToTimeline}
          canEdit={canEdit && !isSavingProject}
          hasProject={Boolean(currentProject)}
        />

        <CanvasPreview
          currentTime={currentTime}
          onTimeChange={setCurrentTime}
          timelineDuration={timelineDuration}
          previewMaterial={previewMaterial}
        />

        <PropertyInspector
          selectedClip={selectedClip}
          projectId={currentProject?.id || null}
          onTriggerRender={handleTriggerRender}
          apiBase={API_BASE}
          canRender={canEdit && !isSavingProject && Boolean(currentProject?.timeline.tracks.some(
            (track) => track.type === "video" && track.clips.length > 0,
          ))}
        />
      </main>

      {/* Timeline panel */}
      <Timeline
        timeline={currentProject?.timeline || { version: "1.1", tracks: [] }}
        selectedClipId={selectedClip?.id || null}
        onSelectClip={setSelectedClip}
        currentTime={currentTime}
      />
    </div>
  );
}
