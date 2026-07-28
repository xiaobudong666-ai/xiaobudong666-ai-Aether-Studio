import React, { useState } from "react";
import { MaterialDTO, RationalTime } from "@aether/contracts";

interface AssetLibraryProps {
  materials: MaterialDTO[];
  onAddMaterial: (material: MaterialDTO) => void;
  onAddClipToTimeline: (materialId: string) => void;
}

export const AssetLibrary: React.FC<AssetLibraryProps> = ({
  materials,
  onAddMaterial,
  onAddClipToTimeline,
}) => {
  const [name, setName] = useState("");
  const [type, setType] = useState<"video" | "audio" | "image">("video");
  const [durationMs, setDurationMs] = useState("5000");

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;

    const ms = parseInt(durationMs) || 5000;
    // Create new material utilizing RationalTime
    const rt = RationalTime.fromMilliseconds(ms, 24000);

    const newMaterial: MaterialDTO = {
      id: "mat-" + Math.random().toString(36).substr(2, 9),
      name: name.trim(),
      url: `https://example.com/assets/${name.trim()}`,
      type: type,
      duration: rt.toJSON(),
    };

    onAddMaterial(newMaterial);
    setName("");
  };

  return (
    <div className="panel" style={{ height: "100%" }}>
      <div className="panel-header">Library & Materials</div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
        {/* Creation Form */}
        <form onSubmit={handleCreate} style={{ display: "flex", flexDirection: "column", gap: "8px", borderBottom: "1px solid #2e2e33", paddingBottom: "12px" }}>
          <div style={{ fontWeight: 600, fontSize: "12px", color: "#a78bfa" }}>Add New Mock Material</div>
          <input
            type="text"
            placeholder="Material name (e.g. intro.mp4)"
            value={name}
            onChange={(e) => setName(e.target.value)}
            style={{ width: "100%", boxSizing: "border-box" }}
          />
          <div style={{ display: "flex", gap: "6px" }}>
            <select value={type} onChange={(e) => setType(e.target.value as any)} style={{ flex: 1 }}>
              <option value="video">Video</option>
              <option value="audio">Audio</option>
              <option value="image">Image</option>
            </select>
            <input
              type="number"
              placeholder="Duration (ms)"
              value={durationMs}
              onChange={(e) => setDurationMs(e.target.value)}
              style={{ width: "90px" }}
            />
          </div>
          <button type="submit" style={{ width: "100%" }}>Add Material</button>
        </form>

        {/* List of Materials */}
        <div style={{ flex: 1, overflowY: "auto" }}>
          <div style={{ fontWeight: 600, fontSize: "12px", color: "#9ca3af", marginBottom: "8px" }}>Project Assets</div>
          {materials.length === 0 ? (
            <div style={{ color: "#71717a", fontStyle: "italic" }}>No assets loaded. Add one above!</div>
          ) : (
            materials.map((m) => {
              const seconds = m.duration ? m.duration.value / m.duration.timescale : 0;
              return (
                <div key={m.id} className="material-card" style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", gap: "4px" }}>
                  <div style={{ fontWeight: 500, wordBreak: "break-all" }}>{m.name}</div>
                  <div style={{ fontSize: "11px", color: "#a1a1aa" }}>
                    Type: {m.type} | Duration: {seconds.toFixed(2)}s ({m.duration?.value}/{m.duration?.timescale})
                  </div>
                  <button
                    className="secondary"
                    onClick={() => onAddClipToTimeline(m.id)}
                    style={{ padding: "3px 6px", fontSize: "11px", alignSelf: "flex-end", marginTop: "4px" }}
                  >
                    + Place on Track
                  </button>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
};
