import React, { useRef, useState } from "react";
import { MaterialDTO } from "@aether/contracts";
import { formatBytes, materialTypeLabel, safeErrorMessage } from "../i18n";

interface AssetLibraryProps {
  materials: MaterialDTO[];
  onUploadMaterial: (file: File) => Promise<void>;
  onAddClipToTimeline: (materialId: string) => Promise<void>;
  canEdit: boolean;
  hasProject: boolean;
}

export const AssetLibrary: React.FC<AssetLibraryProps> = ({
  materials,
  onUploadMaterial,
  onAddClipToTimeline,
  canEdit,
  hasProject,
}) => {
  const inputRef = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [placingMaterialId, setPlacingMaterialId] = useState<string | null>(null);

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
      setUploadError(safeErrorMessage(error, "上传失败，请检查文件后重试。"));
    } finally {
      setUploading(false);
    }
  };

  const handlePlaceOnTimeline = async (materialId: string) => {
    setPlacingMaterialId(materialId);
    setUploadError(null);
    try {
      await onAddClipToTimeline(materialId);
    } catch (error) {
      setUploadError(safeErrorMessage(error, "素材添加到时间线失败，请重试。"));
    } finally {
      setPlacingMaterialId(null);
    }
  };

  return (
    <div className="panel" style={{ height: "100%" }}>
      <div className="panel-header">素材库</div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
        <form onSubmit={handleUpload} style={{ display: "flex", flexDirection: "column", gap: "8px", borderBottom: "1px solid #2e2e33", paddingBottom: "12px" }}>
          <div style={{ fontWeight: 600, fontSize: "12px", color: "#a78bfa" }}>上传真实媒体</div>
          <input
            ref={inputRef}
            type="file"
            className="native-file-input"
            aria-label="媒体文件输入"
            accept="video/*,audio/*,.mkv,.m4v"
            onChange={(e) => setSelectedFile(e.target.files?.[0] || null)}
            disabled={!canEdit || !hasProject}
          />
          <div className="file-picker-row">
            <button
              type="button"
              className="secondary file-picker-button"
              onClick={() => inputRef.current?.click()}
              disabled={!canEdit || !hasProject}
            >
              {selectedFile ? "重新选择文件" : "选择媒体文件"}
            </button>
            <span className="selected-file-name" aria-live="polite">
              {selectedFile ? `已选择：${selectedFile.name}` : "尚未选择文件"}
            </span>
          </div>
          <button type="submit" disabled={!canEdit || !hasProject || !selectedFile || uploading} style={{ width: "100%" }}>
            {uploading ? "正在上传并检测媒体信息…" : "上传媒体"}
          </button>
          {!canEdit && <div style={{ color: "#a1a1aa", fontSize: "12px" }}>当前为只读权限，不能修改项目。</div>}
          {canEdit && !hasProject && <div style={{ color: "#a1a1aa", fontSize: "12px" }}>请先创建或选择一个项目。</div>}
          {uploadError && <div role="alert" style={{ color: "#fca5a5", fontSize: "12px" }}>{uploadError}</div>}
        </form>

        {/* List of Materials */}
        <div style={{ flex: 1, overflowY: "auto" }}>
          <div style={{ fontWeight: 600, fontSize: "12px", color: "#9ca3af", marginBottom: "8px" }}>项目素材</div>
          {materials.length === 0 ? (
            <div style={{ color: "#a1a1aa", fontStyle: "italic" }}>还没有上传素材。</div>
          ) : (
            materials.map((m) => {
              const seconds = m.duration ? m.duration.value / m.duration.timescale : 0;
              return (
                <div key={m.id} className="material-card" style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", gap: "4px" }}>
                  <div style={{ fontWeight: 500, wordBreak: "break-all" }}>{m.name}</div>
                  <div style={{ fontSize: "12px", color: "#a1a1aa" }}>
                    类型：{materialTypeLabel(m.type)} · 时长：{seconds.toFixed(2)} 秒 · 大小：{formatBytes(m.sizeBytes || 0)}
                  </div>
                  <button
                    className="secondary"
                    onClick={() => handlePlaceOnTimeline(m.id)}
                    disabled={!canEdit || placingMaterialId !== null}
                    style={{ padding: "5px 8px", fontSize: "12px", alignSelf: "flex-end", marginTop: "4px" }}
                  >
                    {placingMaterialId === m.id ? "正在添加…" : "+ 添加到时间线"}
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
