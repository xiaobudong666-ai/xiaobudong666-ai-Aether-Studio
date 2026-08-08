import React, { useRef, useState } from "react";
import { MaterialDTO } from "@aether/contracts";

interface AssetLibraryProps {
  materials: MaterialDTO[];
  onUploadMaterial: (file: File) => Promise<void>;
  onAddClipToTimeline: (materialId: string) => void;
  canEdit: boolean;
}

export const AssetLibrary: React.FC<AssetLibraryProps> = ({
  materials,
  onUploadMaterial,
  onAddClipToTimeline,
  canEdit,
}) => {
  const inputRef = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedFile) return;
    setUploading(true);
    setUploadError(null);
    try {
      await onUploadMaterial(selectedFile);
      setSelectedFile(null);
      if (inputRef.current) inputRef.current.value = "";
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="panel" style={{ height: "100%" }}>
      <div className="panel-header">Library & Materials</div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
        <form onSubmit={handleUpload} style={{ display: "flex", flexDirection: "column", gap: "8px", borderBottom: "1px solid #2e2e33", paddingBottom: "12px" }}>
          <div style={{ fontWeight: 600, fontSize: "12px", color: "#a78bfa" }}>Upload Real Media</div>
          <input
            ref={inputRef}
            type="file"
            accept="video/*,audio/*,.mkv,.m4v"
            onChange={(e) => setSelectedFile(e.target.files?.[0] || null)}
            disabled={!canEdit}
            style={{ width: "100%", boxSizing: "border-box" }}
          />
          <button type="submit" disabled={!canEdit || !selectedFile || uploading} style={{ width: "100%" }}>
            {uploading ? "Uploading & probing…" : "Upload Media"}
          </button>
          {!canEdit && <div style={{ color: "#a1a1aa", fontSize: "11px" }}>Viewer access is read-only.</div>}
          {uploadError && <div style={{ color: "#ef4444", fontSize: "11px" }}>{uploadError}</div>}
        </form>

        {/* List of Materials */}
        <div style={{ flex: 1, overflowY: "auto" }}>
          <div style={{ fontWeight: 600, fontSize: "12px", color: "#9ca3af", marginBottom: "8px" }}>Project Assets</div>
          {materials.length === 0 ? (
            <div style={{ color: "#71717a", fontStyle: "italic" }}>No media uploaded yet.</div>
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
                    disabled={!canEdit}
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
