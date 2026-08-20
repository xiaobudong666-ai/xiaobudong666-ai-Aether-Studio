import React, { useCallback, useEffect, useState } from "react";
import {
  AssetVersionDTO,
  RightsCheckDTO,
  RightsCheckSchema,
} from "@aether/contracts";
import {
  apiErrorMessage,
  formatBytes,
  materialTypeLabel,
  rightsDecisionLabel,
} from "../i18n";

interface AssetGovernanceProps {
  apiBase: string;
  projectId: string;
  assetVersion: AssetVersionDTO;
  canEdit: boolean;
  onSessionExpired: () => void;
}

type RightsStatus = "ALLOWED" | "DENIED" | "REVOKED" | "UNKNOWN";

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "无限制";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间待确认";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function probeSummary(assetVersion: AssetVersionDTO): string[] {
  const probe = assetVersion.probe;
  const video = probe.video && typeof probe.video === "object"
    ? probe.video as Record<string, unknown>
    : null;
  const audio = probe.audio && typeof probe.audio === "object"
    ? probe.audio as Record<string, unknown>
    : null;
  const result: string[] = [];
  if (typeof probe.durationSeconds === "number") {
    result.push(`时长 ${probe.durationSeconds.toFixed(2)} 秒`);
  }
  if (typeof video?.width === "number" && typeof video?.height === "number") {
    result.push(`画面 ${video.width}×${video.height}`);
  }
  if (typeof video?.codec === "string") result.push(`视频编码 ${video.codec}`);
  if (typeof audio?.codec === "string") result.push(`音频编码 ${audio.codec}`);
  if (typeof audio?.sampleRate === "number") result.push(`采样率 ${audio.sampleRate} Hz`);
  return result;
}

export const AssetGovernance: React.FC<AssetGovernanceProps> = ({
  apiBase,
  projectId,
  assetVersion,
  canEdit,
  onSessionExpired,
}) => {
  const [rights, setRights] = useState<RightsCheckDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [status, setStatus] = useState<RightsStatus>("ALLOWED");
  const [purpose, setPurpose] = useState("EXPORT");
  const [territory, setTerritory] = useState("GLOBAL");
  const [validFrom, setValidFrom] = useState("");
  const [validUntil, setValidUntil] = useState("");
  const [evidenceRef, setEvidenceRef] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  const loadRights = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(
        `${apiBase}/projects/${encodeURIComponent(projectId)}/asset-versions/${encodeURIComponent(assetVersion.id)}/rights-check?purpose=EXPORT`,
      );
      if (response.status === 401) {
        onSessionExpired();
        return;
      }
      if (response.status === 403) {
        setError("当前账号只能查看已加载的信息，无法刷新权利状态。");
        return;
      }
      if (response.status === 404) {
        setError("素材版本已不存在，请刷新项目后重试。");
        return;
      }
      if (!response.ok) throw new Error("权利状态加载失败");
      const parsed = RightsCheckSchema.safeParse(await response.json());
      if (!parsed.success) throw new Error("权利状态响应格式异常");
      setRights(parsed.data);
    } catch {
      setError("权利状态暂时无法加载，请手动刷新。");
    } finally {
      setLoading(false);
    }
  }, [apiBase, assetVersion.id, onSessionExpired, projectId]);

  useEffect(() => {
    void loadRights();
  }, [loadRights]);

  const copyValue = async (label: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(label);
      window.setTimeout(() => setCopied(null), 1500);
    } catch {
      setError("复制失败，请手动选择文本。");
    }
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    const trimmedPurpose = purpose.trim();
    const trimmedTerritory = territory.trim();
    setError(null);
    setSuccess(null);
    if (!trimmedPurpose || !trimmedTerritory) {
      setError("用途和适用地区不能为空。");
      return;
    }
    if (
      validFrom
      && validUntil
      && new Date(validUntil).getTime() <= new Date(validFrom).getTime()
    ) {
      setError("有效期结束时间必须晚于开始时间。");
      return;
    }
    if (!confirmed) {
      setError("请确认本次操作会追加不可变快照。");
      return;
    }

    setSubmitting(true);
    try {
      const response = await fetch(
        `${apiBase}/projects/${encodeURIComponent(projectId)}/asset-versions/${encodeURIComponent(assetVersion.id)}/rights-snapshots`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Aether-CSRF": "1" },
          body: JSON.stringify({
            status,
            purpose: trimmedPurpose,
            territory: trimmedTerritory,
            validFrom: validFrom ? new Date(validFrom).toISOString() : null,
            validUntil: validUntil ? new Date(validUntil).toISOString() : null,
            evidenceRef: evidenceRef.trim() || null,
          }),
        },
      );
      if (response.status === 401) {
        onSessionExpired();
        return;
      }
      if (response.status === 403) {
        setError("当前账号没有记录权利快照的权限。");
        return;
      }
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        setError(apiErrorMessage(payload, "权利快照保存失败，请检查字段后重试。"));
        return;
      }
      setSuccess("新的不可变权利快照已追加。");
      setConfirmed(false);
      setFormOpen(false);
      await loadRights();
    } catch {
      setError("权利快照保存失败，当前内容已保留，可以重试。");
    } finally {
      setSubmitting(false);
    }
  };

  const summaries = probeSummary(assetVersion);
  const snapshot = rights?.snapshot;

  return (
    <details className="governance-section">
      <summary>素材治理 · v{assetVersion.versionNo}</summary>
      <div className="governance-content">
        <div className="governance-grid">
          <span>版本编号</span>
          <span className="mono-value" title={assetVersion.id}>{assetVersion.id.slice(0, 12)}</span>
          <span>SHA-256</span>
          <span className="copy-row">
            <span className="mono-value" title={assetVersion.sha256}>{assetVersion.sha256.slice(0, 12)}</span>
            <button type="button" className="text-button" onClick={() => void copyValue("哈希", assetVersion.sha256)}>
              {copied === "哈希" ? "已复制" : "复制"}
            </button>
          </span>
          <span>媒体类型</span>
          <span>{materialTypeLabel(assetVersion.mediaType)} · {assetVersion.contentType || "未知 MIME"}</span>
          <span>文件大小</span>
          <span>{formatBytes(assetVersion.sizeBytes)}</span>
          <span>检测信息</span>
          <span>{summaries.length ? summaries.join(" · ") : "暂无额外检测信息"}</span>
        </div>

        <div className="rights-header">
          <span>导出权利</span>
          <span className={`status-pill ${rights?.allowed ? "success" : "warning"}`}>
            {loading ? "加载中" : rights ? rightsDecisionLabel(rights.code) : "状态不可用"}
          </span>
        </div>
        {snapshot ? (
          <div className="governance-grid">
            <span>用途 / 地区</span><span>{snapshot.purpose} / {snapshot.territory}</span>
            <span>有效期</span><span>{formatDateTime(snapshot.validFrom)} 至 {formatDateTime(snapshot.validUntil)}</span>
            <span>证据引用</span><span className="break-value">{snapshot.evidenceRef || "未填写"}</span>
            <span>记录时间</span><span>{formatDateTime(snapshot.capturedAt)}</span>
          </div>
        ) : !loading && <div className="empty-note">尚未记录用于导出的权利快照。</div>}

        <div className="button-row">
          <button type="button" className="secondary" onClick={() => void loadRights()} disabled={loading || submitting}>
            {loading ? "正在刷新…" : "刷新权利"}
          </button>
          {canEdit && (
            <button type="button" onClick={() => setFormOpen((open) => !open)} disabled={submitting}>
              {formOpen ? "收起表单" : "记录权利快照"}
            </button>
          )}
        </div>
        {!canEdit && <div className="empty-note">只读成员可以查看，但不能追加权利快照。</div>}

        {formOpen && canEdit && (
          <form className="governance-form" onSubmit={handleSubmit}>
            <label>权利状态
              <select value={status} onChange={(event) => setStatus(event.target.value as RightsStatus)}>
                <option value="ALLOWED">允许</option>
                <option value="DENIED">拒绝</option>
                <option value="REVOKED">撤销</option>
                <option value="UNKNOWN">待确认</option>
              </select>
            </label>
            <label>用途<input value={purpose} onChange={(event) => setPurpose(event.target.value)} required /></label>
            <label>适用地区<input value={territory} onChange={(event) => setTerritory(event.target.value)} required /></label>
            <label>生效时间<input type="datetime-local" value={validFrom} onChange={(event) => setValidFrom(event.target.value)} /></label>
            <label>结束时间<input type="datetime-local" value={validUntil} onChange={(event) => setValidUntil(event.target.value)} /></label>
            <label>证据引用<input value={evidenceRef} onChange={(event) => setEvidenceRef(event.target.value)} maxLength={2000} /></label>
            <label className="confirmation-row">
              <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
              确认追加新的不可变快照；历史证据不会被编辑或覆盖。
            </label>
            <button type="submit" disabled={submitting || !confirmed}>
              {submitting ? "正在保存…" : "确认追加快照"}
            </button>
          </form>
        )}
        {success && <div className="inline-success" role="status">{success}</div>}
        {error && <div className="inline-error" role="alert">{error}</div>}
      </div>
    </details>
  );
};
