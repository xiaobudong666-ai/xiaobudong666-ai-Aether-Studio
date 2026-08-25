export type GenerationRole = "owner" | "editor" | "viewer";
export type GenerationStatus =
  | "DRAFT" | "PREFLIGHT" | "BLOCKED" | "QUEUED" | "RUNNING"
  | "SUCCEEDED" | "FAILED" | "CANCELLED" | "EXPIRED";

export interface GenerationInput {
  tenantId: string;
  projectId: string;
  prompt: string;
  inputAssetIds: string[];
  aspectRatio: "16:9" | "9:16" | "1:1";
  durationMs: number;
  outputCount: number;
  rightsSnapshotIds: string[];
  role: GenerationRole;
  quotaAvailable?: boolean;
  expectedRevision?: number;
  currentRevision?: number;
}

export interface GenerationRequest extends GenerationInput {
  id: string;
  clientRequestId: string;
  createdBy: string;
  createdAt: string;
}

export interface GenerationAttempt {
  attempt: number;
  status: GenerationStatus;
  createdAt: string;
  updatedAt: string;
  errorCode?: string;
  errorMessage?: string;
}

export interface GenerationResult {
  id: string;
  taskId: string;
  assetVersionId: string;
  sourceUri: string;
  checksum: string;
  mimeType: "video/mp4";
  durationMs: number;
  width: number;
  height: number;
  rightsSnapshotId: string;
  provenance: string;
  createdAt: string;
}

export interface GenerationTask {
  id: string;
  request: GenerationRequest;
  status: GenerationStatus;
  progress: number;
  attempt: number;
  attempts: GenerationAttempt[];
  results: GenerationResult[];
  errorCode?: string;
  errorMessage?: string;
  requiresPreflight?: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface PreflightResult {
  allowed: boolean;
  status: "PREFLIGHT" | "BLOCKED";
  errors: string[];
}

export interface EditorReference {
  id: string;
  projectId: string;
  assetVersionId: string;
  resultId: string;
  adopted: false;
  createdAt: string;
}

export interface AuditEvent {
  action: string;
  actor: string;
  at: string;
  taskId?: string;
}

export interface GenerationAdapterSnapshot {
  version: 1;
  tasks: GenerationTask[];
  editorReferences: EditorReference[];
  audit: AuditEvent[];
}

const SUPPORTED_RATIOS = new Set(["16:9", "9:16", "1:1"]);
const MAX_OUTPUTS = 4;

export function preflightGeneration(input: GenerationInput): PreflightResult {
  const errors: string[] = [];
  if (input.role === "viewer") errors.push("PERMISSION_DENIED");
  if (!input.tenantId || !input.projectId) errors.push("SCOPE_MISSING");
  if (!input.prompt.trim()) errors.push("PROMPT_REQUIRED");
  if (!SUPPORTED_RATIOS.has(input.aspectRatio)) errors.push("UNSUPPORTED_ASPECT_RATIO");
  if (!Number.isInteger(input.durationMs) || input.durationMs < 1000 || input.durationMs > 60000) {
    errors.push("INVALID_DURATION");
  }
  if (!Number.isInteger(input.outputCount) || input.outputCount < 1 || input.outputCount > MAX_OUTPUTS) {
    errors.push("OUTPUT_LIMIT_EXCEEDED");
  }
  if (input.rightsSnapshotIds.length === 0) errors.push("RIGHTS_SNAPSHOT_REQUIRED");
  if (input.quotaAvailable === false) errors.push("QUOTA_EXCEEDED");
  if (input.expectedRevision !== undefined && input.currentRevision !== undefined
    && input.expectedRevision !== input.currentRevision) errors.push("REVISION_CONFLICT");
  return { allowed: errors.length === 0, status: errors.length ? "BLOCKED" : "PREFLIGHT", errors };
}

export function deterministicChecksum(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  const word = (hash >>> 0).toString(16).padStart(8, "0");
  return word.repeat(8);
}

export function safeGenerationError(value: unknown): string {
  const message = value instanceof Error ? value.message : String(value);
  return /api[_-]?key|token|secret|bearer/i.test(message) ? "生成任务失败，技术详情已隐藏。" : message;
}

export function paginateTasks(tasks: GenerationTask[], page: number, pageSize: number): GenerationTask[] {
  return tasks.slice(Math.max(0, page - 1) * pageSize, Math.max(0, page) * pageSize);
}

export class DeterministicGenerationAdapter {
  private readonly tasksById = new Map<string, GenerationTask>();
  private readonly requestToTask = new Map<string, string>();
  private readonly resultChecksums = new Set<string>();
  private readonly editorReferences = new Map<string, EditorReference>();
  readonly audit: AuditEvent[] = [];
  private sequence = 0;
  private readonly now: () => string;

  constructor(now: () => string = () => new Date().toISOString()) {
    this.now = now;
  }

  static restore(snapshot: unknown, now: () => string = () => new Date().toISOString()): DeterministicGenerationAdapter {
    const adapter = new DeterministicGenerationAdapter(now);
    if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) return adapter;
    const candidate = snapshot as Partial<GenerationAdapterSnapshot>;
    if (candidate.version !== 1 || !Array.isArray(candidate.tasks)
      || !Array.isArray(candidate.editorReferences) || !Array.isArray(candidate.audit)) return adapter;
    for (const task of candidate.tasks) {
      if (!task || typeof task.id !== "string" || !task.request?.clientRequestId) continue;
      const clone = JSON.parse(JSON.stringify(task)) as GenerationTask;
      adapter.tasksById.set(clone.id, clone);
      adapter.requestToTask.set(clone.request.clientRequestId, clone.id);
      clone.results.forEach((result) => adapter.resultChecksums.add(result.checksum));
      const sequence = Number(clone.request.id.split("-").at(-1));
      if (Number.isSafeInteger(sequence)) adapter.sequence = Math.max(adapter.sequence, sequence);
    }
    for (const reference of candidate.editorReferences) {
      if (reference?.resultId) adapter.editorReferences.set(reference.resultId, { ...reference });
    }
    adapter.audit.push(...candidate.audit.filter((event) => Boolean(event?.action && event?.actor && event?.at)));
    return adapter;
  }

  snapshot(): GenerationAdapterSnapshot {
    return {
      version: 1,
      tasks: this.list(),
      editorReferences: this.listEditorReferences(),
      audit: this.audit.map((event) => ({ ...event })),
    };
  }

  listEditorReferences(): EditorReference[] {
    return [...this.editorReferences.values()].map((reference) => ({ ...reference }));
  }

  submit(input: GenerationInput, clientRequestId: string, actor: string): GenerationTask {
    const existingId = this.requestToTask.get(clientRequestId);
    if (existingId) return this.clone(this.requireTask(existingId));
    const check = preflightGeneration(input);
    if (!check.allowed) throw new Error(check.errors.join(","));
    const at = this.now();
    const request: GenerationRequest = {
      ...input,
      id: `generation-request-${++this.sequence}`,
      clientRequestId,
      createdBy: actor,
      createdAt: at,
    };
    const task: GenerationTask = {
      id: `generation-task-${this.sequence}`,
      request,
      status: "QUEUED",
      progress: 0,
      attempt: 1,
      attempts: [{ attempt: 1, status: "QUEUED", createdAt: at, updatedAt: at }],
      results: [],
      createdAt: at,
      updatedAt: at,
    };
    this.tasksById.set(task.id, task);
    this.requestToTask.set(clientRequestId, task.id);
    this.record("SUBMIT", actor, task.id);
    return this.clone(task);
  }

  start(taskId: string, actor: string): GenerationTask {
    const task = this.requireTask(taskId);
    if (task.requiresPreflight) throw new Error("PREFLIGHT_REQUIRED");
    if (task.status !== "QUEUED") return this.clone(task);
    this.setStatus(task, "RUNNING", 25);
    this.record("START", actor, task.id);
    return this.clone(task);
  }

  complete(taskId: string, attempt: number, tenantId: string, projectId: string, actor: string): GenerationTask {
    const task = this.requireTask(taskId);
    if (task.status !== "RUNNING" || task.attempt !== attempt) return this.clone(task);
    if (task.request.tenantId !== tenantId || task.request.projectId !== projectId) return this.clone(task);
    const results: GenerationResult[] = [];
    for (let index = 0; index < task.request.outputCount; index += 1) {
      const checksum = deterministicChecksum(`${task.id}:${attempt}:${index}`);
      if (this.resultChecksums.has(checksum)) {
        task.status = "BLOCKED";
        task.errorCode = "CHECKSUM_CONFLICT";
        task.errorMessage = "结果校验值冲突，已阻断。";
        task.updatedAt = this.now();
        return this.clone(task);
      }
      this.resultChecksums.add(checksum);
      const at = this.now();
      results.push({
        id: `generation-result-${task.id}-${attempt}-${index + 1}`,
        taskId: task.id,
        assetVersionId: `governed-version-${checksum.slice(0, 12)}`,
        sourceUri: `local://generation/${task.id}/${attempt}/${index + 1}`,
        checksum,
        mimeType: "video/mp4",
        durationMs: task.request.durationMs,
        width: task.request.aspectRatio === "9:16" ? 1080 : task.request.aspectRatio === "1:1" ? 1080 : 1920,
        height: task.request.aspectRatio === "16:9" ? 1080 : task.request.aspectRatio === "1:1" ? 1080 : 1920,
        rightsSnapshotId: task.request.rightsSnapshotIds[0],
        provenance: `deterministic-local-adapter:${task.request.clientRequestId}:attempt-${attempt}`,
        createdAt: at,
      });
    }
    task.results = results;
    this.setStatus(task, "SUCCEEDED", 100);
    this.record("COMPLETE", actor, task.id);
    return this.clone(task);
  }

  fail(taskId: string, errorCode: string, error: unknown, retryable: boolean, actor: string): GenerationTask {
    const task = this.requireTask(taskId);
    if (task.status !== "RUNNING") return this.clone(task);
    task.errorCode = retryable ? errorCode : `NON_RETRYABLE_${errorCode}`;
    task.errorMessage = safeGenerationError(error);
    this.setStatus(task, "FAILED", task.progress);
    this.record("FAIL", actor, task.id);
    return this.clone(task);
  }

  cancel(taskId: string, actor: string): GenerationTask {
    const task = this.requireTask(taskId);
    if (task.status !== "QUEUED" && task.status !== "RUNNING") return this.clone(task);
    this.setStatus(task, "CANCELLED", task.progress);
    this.record("CANCEL", actor, task.id);
    return this.clone(task);
  }

  retry(taskId: string, actor: string): GenerationTask {
    const task = this.requireTask(taskId);
    if (task.status !== "FAILED" || task.errorCode?.startsWith("NON_RETRYABLE_")) return this.clone(task);
    task.attempt += 1;
    const at = this.now();
    task.attempts.push({ attempt: task.attempt, status: "QUEUED", createdAt: at, updatedAt: at });
    task.errorCode = undefined;
    task.errorMessage = undefined;
    this.setStatus(task, "QUEUED", 0);
    this.record("RETRY", actor, task.id);
    return this.clone(task);
  }

  conflict(taskId: string, actor: string): GenerationTask {
    const task = this.requireTask(taskId);
    task.status = "BLOCKED";
    task.requiresPreflight = true;
    task.errorCode = "REVISION_CONFLICT";
    task.updatedAt = this.now();
    this.record("CONFLICT", actor, task.id);
    return this.clone(task);
  }

  reviewResult(taskId: string, resultId: string, tenantId: string, projectId: string, actor: string): EditorReference {
    const task = this.requireTask(taskId);
    if (task.request.tenantId !== tenantId || task.request.projectId !== projectId) throw new Error("SCOPE_MISMATCH");
    const result = task.results.find((candidate) => candidate.id === resultId);
    if (!result) throw new Error("RESULT_NOT_FOUND");
    if (!result.rightsSnapshotId) throw new Error("RIGHTS_SNAPSHOT_REQUIRED");
    if (!result.provenance) throw new Error("PROVENANCE_REQUIRED");
    const existing = this.editorReferences.get(result.id);
    if (existing) return { ...existing };
    const reference: EditorReference = {
      id: `editor-reference-${result.id}`,
      projectId,
      assetVersionId: result.assetVersionId,
      resultId: result.id,
      adopted: false,
      createdAt: this.now(),
    };
    this.editorReferences.set(result.id, reference);
    this.record("CREATE_EDITOR_REFERENCE", actor, task.id);
    return { ...reference };
  }

  get(taskId: string): GenerationTask { return this.clone(this.requireTask(taskId)); }
  list(): GenerationTask[] { return [...this.tasksById.values()].map((task) => this.clone(task)); }

  private setStatus(task: GenerationTask, status: GenerationStatus, progress: number) {
    task.status = status;
    task.progress = progress;
    task.updatedAt = this.now();
    const attempt = task.attempts.find((candidate) => candidate.attempt === task.attempt);
    if (attempt) {
      attempt.status = status;
      attempt.updatedAt = task.updatedAt;
      attempt.errorCode = task.errorCode;
      attempt.errorMessage = task.errorMessage;
    }
  }

  private record(action: string, actor: string, taskId?: string) {
    this.audit.push({ action, actor, taskId, at: this.now() });
  }

  private requireTask(taskId: string): GenerationTask {
    const task = this.tasksById.get(taskId);
    if (!task) throw new Error("TASK_NOT_FOUND");
    return task;
  }

  private clone(task: GenerationTask): GenerationTask {
    return JSON.parse(JSON.stringify(task)) as GenerationTask;
  }
}
