import { useState, useEffect } from "react";
import { RationalTime, ProjectDTO, ClipDTO } from "@aether/contracts";
import { AssetLibrary } from "./components/AssetLibrary";
import { CanvasPreview } from "./components/CanvasPreview";
import { PropertyInspector } from "./components/PropertyInspector";
import { Timeline } from "./components/Timeline";

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

  // Production uses the same-origin Nginx /api proxy. Local Vite mirrors it.
  const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";
  const OPENREEL_URL = (import.meta.env.VITE_OPENREEL_URL || "").trim();
  const canEdit = authUser?.role === "owner" || authUser?.role === "editor";

  const stateHeaders = { "X-Aether-CSRF": "1" };

  const loadIdentity = async () => {
    try {
      const response = await fetch(`${API_BASE}/auth/me`);
      if (!response.ok) {
        setAuthUser(null);
        return;
      }
      setAuthUser(await response.json());
    } finally {
      setAuthLoading(false);
    }
  };

  // 1. Fetch projects on load
  const fetchProjects = async () => {
    try {
      const res = await fetch(`${API_BASE}/projects`);
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
        throw new Error(payload?.detail?.message || "Failed to fetch projects");
      }
    } catch (err) {
      setApiError(err instanceof Error ? err.message : "Backend unavailable");
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
    setLoginError(null);
    try {
      const response = await fetch(`${API_BASE}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: loginEmail, password: loginPassword }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        setLoginError(payload?.detail?.message || "Unable to sign in");
        return;
      }
      setAuthUser(await response.json());
      setLoginPassword("");
    } catch {
      setLoginError("The sign-in service is unavailable");
    }
  };

  const handleLogout = async () => {
    await fetch(`${API_BASE}/auth/logout`, { method: "POST", headers: stateHeaders });
    setAuthUser(null);
    setProjects([]);
    setCurrentProject(null);
  };

  const fetchProjectDetail = async (id: string) => {
    try {
      const res = await fetch(`${API_BASE}/projects/${id}`);
      if (res.ok) {
        const data = await res.json();
        setCurrentProject(data);
      }
    } catch (err) {
      console.error(err);
    }
  };

  // 2. Create a new project
  const handleCreateProject = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProjectName.trim()) return;

    try {
      const res = await fetch(`${API_BASE}/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...stateHeaders },
        body: JSON.stringify({ name: newProjectName.trim() }),
      });
      if (res.ok) {
        const newProj = await res.json();
        setProjects((prev) => [...prev, newProj]);
        setCurrentProject(newProj);
        setNewProjectName("");
      } else {
        const payload = await res.json().catch(() => null);
        setApiError(payload?.detail?.message || "Project creation failed");
      }
    } catch (err) {
      setApiError(err instanceof Error ? err.message : "Project creation failed");
    }
  };

  // 3. Save current project (Update) with optimistic lock checks
  const saveProjectState = async (updatedProj: ProjectDTO) => {
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
      if (res.status === 409) {
        alert("Concurrency Conflict Detected! Your project was edited by another process. Reloading the latest server state.");
        fetchProjectDetail(updatedProj.id);
      } else if (res.ok) {
        const latest = await res.json();
        setCurrentProject(latest);
      } else {
        const payload = await res.json().catch(() => null);
        throw new Error(payload?.detail?.message || "Project save failed");
      }
    } catch (err) {
      setApiError(err instanceof Error ? err.message : "Project save failed");
    }
  };

  // 4. Upload and probe real media through the isolated video-use service.
  const handleUploadMaterial = async (file: File) => {
    if (!currentProject) throw new Error("Create or select a project first");
    const data = new FormData();
    data.append("expectedRevision", String(currentProject.revision));
    data.append("file", file);
    const response = await fetch(`${API_BASE}/projects/${currentProject.id}/media`, {
      method: "POST",
      headers: stateHeaders,
      body: data,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new Error(payload?.detail?.message || payload?.detail || "Media upload failed");
    }
    const payload = await response.json();
    const updatedProject = payload.project as ProjectDTO;
    setCurrentProject(updatedProject);
    setProjects((prev) => prev.map((project) => (
      project.id === updatedProject.id ? updatedProject : project
    )));
  };

  // 5. Place material on timeline as a clip
  const handleAddClipToTimeline = (materialId: string) => {
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
        name: `${material.type.charAt(0).toUpperCase() + material.type.slice(1)} Track 1`,
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

    saveProjectState(updatedProj);
  };

  // 6. Trigger backend render task
  const handleTriggerRender = async () => {
    if (!currentProject) return;
    const res = await fetch(`${API_BASE}/projects/${currentProject.id}/render`, {
      method: "POST",
      headers: stateHeaders,
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      throw new Error(payload?.detail?.message || "Trigger render failed");
    }
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

  if (authLoading) {
    return <div className="auth-screen"><div className="auth-card">Loading Aether Studio…</div></div>;
  }

  if (!authUser) {
    return (
      <div className="auth-screen">
        <form className="auth-card" onSubmit={handleLogin}>
          <div className="auth-mark">✨</div>
          <h1>Aether Studio</h1>
          <p>Sign in to your protected workspace</p>
          <label>Email<input aria-label="Email" type="email" value={loginEmail} onChange={(event) => setLoginEmail(event.target.value)} required /></label>
          <label>Password<input aria-label="Password" type="password" value={loginPassword} onChange={(event) => setLoginPassword(event.target.value)} required /></label>
          {loginError && <div className="auth-error" role="alert">{loginError}</div>}
          <button type="submit">Sign in</button>
        </form>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh" }}>
      {/* Header */}
      <header className="editor-header">
        <div className="editor-logo">
          <span>✨</span> Aether Studio AI Anime Workbench
          <span style={{ fontSize: "11px", color: "#a1a1aa", marginLeft: "8px" }}>
            OpenCut Core 0.2.10
          </span>
        </div>
        <div className="project-select-container">
          <span className="tenant-badge" title={`${authUser.role} · ${authUser.email}`}>
            {authUser.tenant.name} · {authUser.role}
          </span>
          {apiError && <span style={{ fontSize: "12px", color: "#f59e0b" }}>⚠️ {apiError}</span>}
          <form onSubmit={handleCreateProject} style={{ display: "flex", gap: "6px" }}>
            <input
              type="text"
              placeholder="New project name"
              value={newProjectName}
              onChange={(e) => setNewProjectName(e.target.value)}
              disabled={!canEdit}
            />
            <button type="submit" disabled={!canEdit}>Create Project</button>
          </form>
          <select
            value={currentProject?.id || ""}
            onChange={(e) => fetchProjectDetail(e.target.value)}
          >
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} (r{p.revision})
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={!currentProject}
            onClick={handleExportOpenCutSnapshot}
            title="Export a pinned OpenCut Classic compatibility snapshot and media manifest"
          >
            Export OpenCut Snapshot
          </button>
          <button
            type="button"
            disabled={!currentProject}
            onClick={handleExportOpenReelProject}
            title="Export an OpenReel schema 1.0.0 project file with relinkable media placeholders"
          >
            Export OpenReel Project
          </button>
          {OPENREEL_URL && (
            <a href={OPENREEL_URL} target="_blank" rel="noreferrer noopener">
              Open OpenReel
            </a>
          )}
          <button type="button" className="secondary" onClick={handleLogout}>Sign out</button>
        </div>
      </header>

      {/* Main workbench */}
      <main className="workbench-container">
        <AssetLibrary
          materials={currentProject?.materials || []}
          onUploadMaterial={handleUploadMaterial}
          onAddClipToTimeline={handleAddClipToTimeline}
          canEdit={canEdit}
        />

        <CanvasPreview
          currentTime={currentTime}
          onTimeChange={setCurrentTime}
          timelineDuration={timelineDuration}
        />

        <PropertyInspector
          selectedClip={selectedClip}
          projectId={currentProject?.id || null}
          onTriggerRender={handleTriggerRender}
          apiBase={API_BASE}
          canRender={canEdit && Boolean(currentProject?.timeline.tracks.some(
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
