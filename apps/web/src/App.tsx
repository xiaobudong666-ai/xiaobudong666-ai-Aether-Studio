import { useState, useEffect } from "react";
import { RationalTime, ProjectDTO, MaterialDTO, ClipDTO } from "@aether/contracts";
import { AssetLibrary } from "./components/AssetLibrary";
import { CanvasPreview } from "./components/CanvasPreview";
import { PropertyInspector } from "./components/PropertyInspector";
import { Timeline } from "./components/Timeline";

export default function App() {
  const [projects, setProjects] = useState<ProjectDTO[]>([]);
  const [currentProject, setCurrentProject] = useState<ProjectDTO | null>(null);
  const [newProjectName, setNewProjectName] = useState("");
  const [selectedClip, setSelectedClip] = useState<ClipDTO | null>(null);
  const [currentTime, setCurrentTime] = useState<RationalTime>(new RationalTime(0, 24000));
  const [apiError, setApiError] = useState<string | null>(null);

  // Production uses the same-origin Nginx /api proxy. Local Vite mirrors it.
  const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";

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
        throw new Error("Failed to fetch projects");
      }
    } catch (err: any) {
      console.warn("Backend API not reachable. Using mock client-side projects.", err);
      setApiError("Backend offline - Using local mockup state");
      // Load fallback local project so frontend is always interactive
      if (projects.length === 0) {
        const fallbackProject: ProjectDTO = {
          id: "local-demo-project",
          name: "Local Mockup Project",
          timeline: {
            version: "1.1",
            tracks: [
              {
                id: "track-1",
                name: "Video Track 1",
                type: "video",
                clips: []
              }
            ]
          },
          materials: [
            {
              id: "mat-1",
              name: "Welcome_Anime.mp4",
              url: "https://example.com/assets/Welcome_Anime.mp4",
              type: "video",
              duration: { value: 120000, timescale: 24000 } // 5 seconds
            }
          ],
          revision: 1,
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
        };
        setProjects([fallbackProject]);
        setCurrentProject(fallbackProject);
      }
    }
  };

  useEffect(() => {
    fetchProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newProjectName.trim() }),
      });
      if (res.ok) {
        const newProj = await res.json();
        setProjects((prev) => [...prev, newProj]);
        setCurrentProject(newProj);
        setNewProjectName("");
      }
    } catch (err) {
      // Fallback
      const newProj: ProjectDTO = {
        id: "local-" + Math.random().toString(36).substr(2, 9),
        name: newProjectName.trim(),
        timeline: { version: "1.1", tracks: [] },
        materials: [],
        revision: 1,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      };
      setProjects((prev) => [...prev, newProj]);
      setCurrentProject(newProj);
      setNewProjectName("");
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
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: updatedProj.name,
          timeline: updatedProj.timeline,
          materials: updatedProj.materials,
          expectedRevision: updatedProj.revision - 1, // Prior revision
        }),
      });
      if (res.status === 409) {
        alert("Concurrency Conflict Detected! Your project was edited by another process. Reloading the latest server state.");
        fetchProjectDetail(updatedProj.id);
      } else if (res.ok) {
        const latest = await res.json();
        setCurrentProject(latest);
      }
    } catch (err) {
      console.warn("Could not save to backend. State kept locally.", err);
    }
  };

  // 4. Add Material to current project
  const handleAddMaterial = (material: MaterialDTO) => {
    if (!currentProject) return;
    const updatedMaterials = [...currentProject.materials, material];
    const updatedProj: ProjectDTO = {
      ...currentProject,
      materials: updatedMaterials,
      revision: currentProject.revision + 1,
      updatedAt: new Date().toISOString(),
    };
    saveProjectState(updatedProj);
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
    });
    if (!res.ok) {
      throw new Error("Trigger render failed");
    }
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

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh" }}>
      {/* Header */}
      <header className="editor-header">
        <div className="editor-logo">
          <span>✨</span> Aether Studio AI Anime Workbench
        </div>
        <div className="project-select-container">
          {apiError && <span style={{ fontSize: "12px", color: "#f59e0b" }}>⚠️ {apiError}</span>}
          <form onSubmit={handleCreateProject} style={{ display: "flex", gap: "6px" }}>
            <input
              type="text"
              placeholder="New project name"
              value={newProjectName}
              onChange={(e) => setNewProjectName(e.target.value)}
            />
            <button type="submit">Create Project</button>
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
        </div>
      </header>

      {/* Main workbench */}
      <main className="workbench-container">
        <AssetLibrary
          materials={currentProject?.materials || []}
          onAddMaterial={handleAddMaterial}
          onAddClipToTimeline={handleAddClipToTimeline}
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
