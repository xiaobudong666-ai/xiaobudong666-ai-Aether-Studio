import React, { useCallback, useEffect, useState } from "react";
import {
  CandidateDTO,
  CandidateSchema,
  MasterRevisionDTO,
  MasterRevisionSchema,
} from "@aether/contracts";
import {
  apiErrorMessage,
  candidateStatusLabel,
  rightsDecisionLabel,
} from "../i18n";

interface FinishedMediaPanelProps {
  apiBase: string;
  projectId: string | null;
  canEdit: boolean;
  onSessionExpired: () => void;
  refreshToken: string;
}

interface AdoptionIntent {
  candidateId: string;
  key: string;
  reason: string;
  confirmed: boolean;
}

interface RightsFailure {
  mediaId?: string;
  assetVersionId?: string;
  code?: string;
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间待确认";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function createIntentKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `adopt-${crypto.randomUUID()}`;
  }
  return `adopt-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

function parseCandidates(payload: unknown): CandidateDTO[] {
  if (!Array.isArray(payload)) throw new Error("候选列表格式异常");
  return payload.map((item) => CandidateSchema.parse(item));
}

function parseMasters(payload: unknown): MasterRevisionDTO[] {
  if (!Array.isArray(payload)) throw new Error("母版列表格式异常");
  return payload.map((item) => MasterRevisionSchema.parse(item));
}

export const FinishedMediaPanel: React.FC<FinishedMediaPanelProps> = ({
  apiBase,
  projectId,
  canEdit,
  onSessionExpired,
  refreshToken,
}) => {
  const [candidates, setCandidates] = useState<CandidateDTO[]>([]);
  const [masters, setMasters] = useState<MasterRevisionDTO[]>([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [rightsFailures, setRightsFailures] = useState<RightsFailure[]>([]);
  const [intent, setIntent] = useState<AdoptionIntent | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  useEffect(() => {
    setCandidates([]);
    setMasters([]);
    setIntent(null);
    setError(null);
    setSuccess(null);
    setRightsFailures([]);
    setCopiedId(null);
  }, [projectId]);

  const loadFinishedMedia = useCallback(async (preserveError = false) => {
    if (!projectId) {
      setCandidates([]);
      setMasters([]);
      setIntent(null);
      setError(null);
      return;
    }
    setLoading(true);
    if (!preserveError) setError(null);
    try {
      const [candidateResponse, masterResponse] = await Promise.all([
        fetch(`${apiBase}/projects/${encodeURIComponent(projectId)}/candidates`),
        fetch(`${apiBase}/projects/${encodeURIComponent(projectId)}/masters`),
      ]);
      if (candidateResponse.status === 401 || masterResponse.status === 401) {
        onSessionExpired();
        return;
      }
      if (candidateResponse.status === 404 || masterResponse.status === 404) {
        setCandidates([]);
        setMasters([]);
        setError("项目已不存在，请重新选择项目。");
        return;
      }
      if (!candidateResponse.ok || !masterResponse.ok) {
        throw new Error("成片数据加载失败");
      }
      const [candidatePayload, masterPayload] = await Promise.all([
        candidateResponse.json(),
        masterResponse.json(),
      ]);
      setCandidates(parseCandidates(candidatePayload));
      setMasters(parseMasters(masterPayload));
    } catch {
      setError("候选成片或母版暂时无法加载，已有数据已保留。");
    } finally {
      setLoading(false);
    }
  }, [apiBase, onSessionExpired, projectId]);

  useEffect(() => {
    void loadFinishedMedia();
  }, [loadFinishedMedia, refreshToken]);

  const beginAdoption = (candidateId: string) => {
    setError(null);
    setSuccess(null);
    setRightsFailures([]);
    setIntent({
      candidateId,
      key: createIntentKey(),
      reason: "",
      confirmed: false,
    });
  };

  const submitAdoption = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!projectId || !intent || submitting) return;
    const reason = intent.reason.trim();
    setError(null);
    setSuccess(null);
    setRightsFailures([]);
    if (!reason) {
      setError("请填写采纳原因。");
      return;
    }
    if (!intent.confirmed) {
      setError("请确认采纳操作在本批次中不可撤销。");
      return;
    }

    setSubmitting(true);
    try {
      const response = await fetch(
        `${apiBase}/projects/${encodeURIComponent(projectId)}/candidates/${encodeURIComponent(intent.candidateId)}/adopt`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Aether-CSRF": "1",
            "Idempotency-Key": intent.key,
          },
          body: JSON.stringify({ reason }),
        },
      );
      if (response.status === 401) {
        onSessionExpired();
        return;
      }
      const payload = await response.json().catch(() => null);
      if (response.status === 403) {
        setError("当前账号没有采纳候选成片的权限。");
        return;
      }
      if (response.status === 404) {
        await loadFinishedMedia(true);
        setError("候选成片已不存在，列表已刷新。");
        return;
      }
      if (response.status === 409) {
        await loadFinishedMedia(true);
        setError(apiErrorMessage(payload, "采纳状态发生冲突，列表已刷新，请核对母版结果。"));
        return;
      }
      if (response.status === 422) {
        const detail = payload && typeof payload === "object"
          ? (payload as { detail?: { failures?: RightsFailure[] } }).detail
          : undefined;
        setRightsFailures(Array.isArray(detail?.failures) ? detail.failures : []);
        setError(apiErrorMessage(payload, "采纳校验未通过，请检查素材权利。"));
        return;
      }
      if (!response.ok) {
        setError(apiErrorMessage(payload, "候选成片采纳失败，可以使用同一操作标识重试。"));
        return;
      }
      const parsed = MasterRevisionSchema.safeParse(payload);
      if (!parsed.success) throw new Error("母版响应格式异常");
      setSuccess(`候选已采纳为母版修订 v${parsed.data.revisionNo}。`);
      setIntent(null);
      await loadFinishedMedia(true);
    } catch {
      setError("采纳请求未完成；本次操作标识已保留，可以安全重试。");
    } finally {
      setSubmitting(false);
    }
  };

  const copyId = async (id: string) => {
    try {
      await navigator.clipboard.writeText(id);
      setCopiedId(id);
      window.setTimeout(() => setCopiedId(null), 1500);
    } catch {
      setError("复制失败，请手动选择编号。");
    }
  };

  return (
    <section className="finished-media-panel" aria-label="候选成片与母版">
      <div className="section-title-row">
        <strong>候选成片与母版</strong>
        <button type="button" className="text-button" onClick={() => void loadFinishedMedia()} disabled={!projectId || loading || submitting}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>
      {!projectId ? (
        <div className="empty-note">请选择项目后查看成片。</div>
      ) : (
        <>
          <div className="subsection-title">候选成片</div>
          {candidates.length === 0 ? (
            <div className="empty-note">还没有可采纳的候选成片。</div>
          ) : candidates.map((candidate) => (
            <article className="operation-card" key={candidate.id}>
              <div className="card-title-row">
                <span title={candidate.id}>候选 {candidate.id.slice(0, 8)}</span>
                <span className={`status-pill ${candidate.status === "ADOPTED" ? "success" : "warning"}`}>
                  {candidateStatusLabel(candidate.status)}
                </span>
              </div>
              <div>任务：<span className="mono-value" title={candidate.taskId}>{candidate.taskId.slice(0, 12)}</span></div>
              <div>输入项目版本：{candidate.inputRevision}</div>
              <div>生成时间：{formatDateTime(candidate.createdAt)}</div>
              <div className="button-row">
                <a href={candidate.artifactRef} download>下载候选成片</a>
                {canEdit && candidate.status === "READY" && (
                  <button type="button" onClick={() => beginAdoption(candidate.id)} disabled={submitting}>
                    采纳为母版
                  </button>
                )}
              </div>
              {!canEdit && candidate.status === "READY" && <div className="empty-note">只读成员不能采纳候选。</div>}
              {intent?.candidateId === candidate.id && (
                <form className="adoption-form" onSubmit={submitAdoption}>
                  <label>采纳原因
                    <textarea
                      value={intent.reason}
                      onChange={(event) => setIntent({ ...intent, reason: event.target.value })}
                      maxLength={2000}
                      required
                    />
                  </label>
                  <div className="intent-key" title={intent.key}>操作标识：{intent.key.slice(0, 22)}…</div>
                  <label className="confirmation-row">
                    <input
                      type="checkbox"
                      checked={intent.confirmed}
                      onChange={(event) => setIntent({ ...intent, confirmed: event.target.checked })}
                    />
                    确认这是一次明确采纳；本批次不提供撤销、替换或发布操作。
                  </label>
                  <div className="button-row">
                    <button type="submit" disabled={submitting || !intent.confirmed}>
                      {submitting ? "正在采纳…" : "确认采纳"}
                    </button>
                    <button type="button" className="secondary" onClick={() => setIntent(null)} disabled={submitting}>取消</button>
                  </div>
                </form>
              )}
            </article>
          ))}

          <div className="subsection-title">母版修订</div>
          {masters.length === 0 ? (
            <div className="empty-note">还没有母版修订。</div>
          ) : masters.map((master) => (
            <article className="operation-card master-card" key={master.id}>
              <div className="card-title-row">
                <strong>母版 v{master.revisionNo}</strong>
                <span>{formatDateTime(master.createdAt)}</span>
              </div>
              <div>母版编号：<span className="mono-value" title={master.id}>{master.id.slice(0, 12)}</span></div>
              <div>来源候选：<span className="mono-value" title={master.adoption.candidateId}>{master.adoption.candidateId.slice(0, 12)}</span></div>
              <div>采纳人：<span className="mono-value" title={master.adoption.adoptedBy}>{master.adoption.adoptedBy.slice(0, 12)}</span></div>
              <div className="break-value">采纳原因：{master.adoption.reason}</div>
              <div>SHA-256：{master.sha256 ? <span className="mono-value" title={master.sha256}>{master.sha256.slice(0, 12)}</span> : "尚未提供"}</div>
              <div className="button-row">
                <a href={master.artifactRef} download>下载母版</a>
                <button type="button" className="secondary" onClick={() => void copyId(master.id)}>
                  {copiedId === master.id ? "已复制" : "复制 ID"}
                </button>
              </div>
            </article>
          ))}
        </>
      )}
      {rightsFailures.length > 0 && (
        <div className="rights-failures" role="alert">
          <strong>未通过权利检查的素材</strong>
          {rightsFailures.map((failure, index) => (
            <div key={`${failure.mediaId || "unknown"}-${index}`}>
              素材 {failure.mediaId || "未知"}：{rightsDecisionLabel(failure.code || "RIGHTS_UNKNOWN")}
              {failure.assetVersionId ? `（版本 ${failure.assetVersionId.slice(0, 8)}）` : ""}
            </div>
          ))}
        </div>
      )}
      {success && <div className="inline-success" role="status">{success}</div>}
      {error && <div className="inline-error" role="alert">{error}</div>}
    </section>
  );
};
