import { describe, expect, test, vi } from "vitest";
import {
  DeterministicGenerationAdapter,
  GenerationApiClient,
  GenerationInput,
  deterministicChecksum,
  paginateTasks,
  preflightGeneration,
  safeGenerationError,
} from "./generation";

const base: GenerationInput = {
  tenantId: "tenant-1",
  projectId: "project-1",
  prompt: "一匹马穿过雨夜城市",
  inputAssetIds: ["asset-1"],
  aspectRatio: "9:16",
  durationMs: 10000,
  outputCount: 1,
  rightsSnapshotIds: ["rights-1"],
  role: "owner",
  quotaAvailable: true,
  expectedRevision: 3,
  currentRevision: 3,
};

function setup() {
  let tick = 0;
  const adapter = new DeterministicGenerationAdapter(() => `2026-08-25T00:00:${String(tick++).padStart(2, "0")}Z`);
  return adapter;
}

function running(adapter: DeterministicGenerationAdapter, input = base) {
  const task = adapter.submit(input, `request-${adapter.list().length + 1}`, "owner-1");
  return adapter.start(task.id, "owner-1");
}

describe("IM9–IM11 28 acceptance cases", () => {
  test("01 缺提示词不可预检", () => {
    expect(preflightGeneration({ ...base, prompt: " " }).errors).toContain("PROMPT_REQUIRED");
  });

  test("02 缺权利快照进入 BLOCKED", () => {
    expect(preflightGeneration({ ...base, rightsSnapshotIds: [] })).toMatchObject({ allowed: false, status: "BLOCKED" });
  });

  test("03 BLOCKED 不产生任务或时间线引用", () => {
    const adapter = setup();
    expect(() => adapter.submit({ ...base, rightsSnapshotIds: [] }, "blocked", "owner-1")).toThrow();
    expect(adapter.list()).toHaveLength(0);
  });

  test("04 重复提交只产生一个 clientRequestId", () => {
    const adapter = setup();
    const first = adapter.submit(base, "same-request", "owner-1");
    const second = adapter.submit(base, "same-request", "owner-1");
    expect(second.id).toBe(first.id);
    expect(adapter.list()).toHaveLength(1);
  });

  test("05 项目切换后迟到响应不污染新项目", () => {
    const adapter = setup();
    const task = running(adapter);
    const late = adapter.complete(task.id, task.attempt, base.tenantId, "project-2", "owner-1");
    expect(late.status).toBe("RUNNING");
    expect(late.results).toHaveLength(0);
  });

  test("06 cancel 后迟到成功结果不恢复任务", () => {
    const adapter = setup();
    const task = running(adapter);
    adapter.cancel(task.id, "owner-1");
    const late = adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1");
    expect(late.status).toBe("CANCELLED");
  });

  test("07 失败重试保留 attempt 历史", () => {
    const adapter = setup();
    const task = running(adapter);
    adapter.fail(task.id, "LOCAL_TIMEOUT", new Error("本地适配器超时"), true, "owner-1");
    const retried = adapter.retry(task.id, "owner-1");
    expect(retried.attempt).toBe(2);
    expect(retried.attempts.map((item) => item.status)).toEqual(["FAILED", "QUEUED"]);
  });

  test("08 409 后必须重新预检", () => {
    const adapter = setup();
    const task = adapter.submit(base, "conflict", "owner-1");
    const conflicted = adapter.conflict(task.id, "owner-1");
    expect(conflicted).toMatchObject({ status: "BLOCKED", requiresPreflight: true });
    expect(() => adapter.start(task.id, "owner-1")).toThrow("PREFLIGHT_REQUIRED");
  });

  test("09 fake adapter 成功结果生成唯一 checksum", () => {
    const adapter = setup();
    const task = running(adapter, { ...base, outputCount: 2 });
    const done = adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1");
    expect(new Set(done.results.map((result) => result.checksum)).size).toBe(2);
    expect(done.results[0].checksum).toHaveLength(64);
  });

  test("10 结果无 rightsSnapshotId 不得审阅通过", () => {
    const adapter = setup();
    const task = running(adapter);
    const done = adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1");
    (adapter as unknown as { tasksById: Map<string, typeof done> }).tasksById.get(task.id)!.results[0].rightsSnapshotId = "";
    expect(() => adapter.reviewResult(task.id, done.results[0].id, base.tenantId, base.projectId, "owner-1")).toThrow("RIGHTS_SNAPSHOT_REQUIRED");
  });

  test("11 Viewer 无提交能力", () => {
    expect(preflightGeneration({ ...base, role: "viewer" }).errors).toContain("PERMISSION_DENIED");
  });

  test("12 跨 tenant/project 请求被拒绝", () => {
    const adapter = setup();
    const task = running(adapter);
    const done = adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1");
    expect(() => adapter.reviewResult(task.id, done.results[0].id, "tenant-2", base.projectId, "owner-1")).toThrow("SCOPE_MISMATCH");
  });

  test("13 进入剪辑仅创建引用且不自动采纳", () => {
    const adapter = setup();
    const task = running(adapter);
    const done = adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1");
    expect(adapter.reviewResult(task.id, done.results[0].id, base.tenantId, base.projectId, "owner-1").adopted).toBe(false);
  });

  test("14 重复结果不会生成重复 AssetVersion 引用", () => {
    const adapter = setup();
    const task = running(adapter);
    const done = adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1");
    const one = adapter.reviewResult(task.id, done.results[0].id, base.tenantId, base.projectId, "owner-1");
    const two = adapter.reviewResult(task.id, done.results[0].id, base.tenantId, base.projectId, "owner-1");
    expect(two.id).toBe(one.id);
  });

  test("15 输出数量达到上限时阻止提交", () => {
    expect(preflightGeneration({ ...base, outputCount: 5 }).errors).toContain("OUTPUT_LIMIT_EXCEEDED");
  });

  test("16 不支持的比例被预检拒绝", () => {
    const input = { ...base, aspectRatio: "4:3" as GenerationInput["aspectRatio"] };
    expect(preflightGeneration(input).errors).toContain("UNSUPPORTED_ASPECT_RATIO");
  });

  test("17 取消只改变可取消状态", () => {
    const adapter = setup();
    const task = adapter.submit(base, "queued", "owner-1");
    expect(adapter.cancel(task.id, "owner-1").status).toBe("CANCELLED");
  });

  test("18 已成功任务不可再次 cancel", () => {
    const adapter = setup();
    const task = running(adapter);
    adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1");
    expect(adapter.cancel(task.id, "owner-1").status).toBe("SUCCEEDED");
  });

  test("19 不可重试失败不会创建新 attempt", () => {
    const adapter = setup();
    const task = running(adapter);
    adapter.fail(task.id, "POLICY", new Error("策略失败"), false, "owner-1");
    expect(adapter.retry(task.id, "owner-1").attempt).toBe(1);
  });

  test("20 列表分页刷新不改变状态机事实", () => {
    const adapter = setup();
    adapter.submit(base, "page-1", "owner-1");
    adapter.submit(base, "page-2", "owner-1");
    const page = paginateTasks(adapter.list(), 2, 1);
    expect(page).toHaveLength(1);
    expect(adapter.list().map((task) => task.status)).toEqual(["QUEUED", "QUEUED"]);
  });

  test("21 错误信息不泄露凭据", () => {
    expect(safeGenerationError(new Error("Bearer secret-token failed"))).toBe("生成任务失败，技术详情已隐藏。");
  });

  test("22 provenance 缺失的结果不可采纳", () => {
    const adapter = setup();
    const task = running(adapter);
    const done = adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1");
    (adapter as unknown as { tasksById: Map<string, typeof done> }).tasksById.get(task.id)!.results[0].provenance = "";
    expect(() => adapter.reviewResult(task.id, done.results[0].id, base.tenantId, base.projectId, "owner-1")).toThrow("PROVENANCE_REQUIRED");
  });

  test("23 checksum 冲突进入 BLOCKED", () => {
    const adapter = setup();
    const task = running(adapter);
    const checksum = deterministicChecksum(`${task.id}:${task.attempt}:0`);
    (adapter as unknown as { resultChecksums: Set<string> }).resultChecksums.add(checksum);
    expect(adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1").status).toBe("BLOCKED");
  });

  test("24 结果审阅与任务项目不一致时禁止继续", () => {
    const adapter = setup();
    const task = running(adapter);
    const done = adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1");
    expect(() => adapter.reviewResult(task.id, done.results[0].id, base.tenantId, "project-2", "owner-1")).toThrow("SCOPE_MISMATCH");
  });

  test("25 关闭页面后可通过版本化本地快照恢复任务状态", () => {
    const adapter = setup();
    const task = running(adapter);
    const persisted = JSON.parse(JSON.stringify(adapter.snapshot()));
    const restored = DeterministicGenerationAdapter.restore(persisted);
    expect(restored.get(task.id)).toMatchObject({ id: task.id, status: "RUNNING", attempt: 1 });
    expect(restored.audit.map((event) => event.action)).toEqual(["SUBMIT", "START"]);
  });

  test("26 同一任务重复 refresh 不重复创建结果", () => {
    const adapter = setup();
    const task = running(adapter);
    const done = adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1");
    const refreshed = adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1");
    expect(refreshed.results).toHaveLength(done.results.length);
  });

  test("27 生成参数快照与结果可追溯", () => {
    const adapter = setup();
    const task = running(adapter);
    const done = adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1");
    expect(done.request.prompt).toBe(base.prompt);
    expect(done.results[0].provenance).toContain(done.request.clientRequestId);
  });

  test("28 所有关键操作记录 actor 与时间", () => {
    const adapter = setup();
    const task = running(adapter);
    adapter.complete(task.id, task.attempt, base.tenantId, base.projectId, "owner-1");
    expect(adapter.audit.map((event) => event.action)).toEqual(["SUBMIT", "START", "COMPLETE"]);
    expect(adapter.audit.every((event) => event.actor === "owner-1" && event.at)).toBe(true);
  });
});

describe("IM12–IM14 generation API client", () => {
  const task = {
    taskId: "server-task-1", projectId: "project-1", provider: "moneyprinter",
    status: "QUEUED", storedStatus: "QUEUED", progress: 0, message: "queued",
    attempt: 1, maxAttempts: 3, cancelRequested: false,
    rights: { allowed: false, code: "RIGHTS_MISSING" }, results: [],
    createdAt: "2026-08-27T00:00:00Z", updatedAt: "2026-08-27T00:00:00Z",
  };

  test("mutations carry CSRF and idempotency headers", async () => {
    const request = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
      void _input;
      void _init;
      return { ok: true, status: 202, json: async () => task } as Response;
    });
    const client = new GenerationApiClient("/api", request);
    await client.create("project-1", {
      videoSubject: "subject", videoAspect: "9:16", voiceName: "voice",
      videoConcatMode: "random", videoClipDuration: 5, outputCount: 1,
      inputAssetVersionIds: [], idempotencyKey: "00000000-0000-4000-8000-000000000001",
      capabilitySnapshotHash: "a".repeat(64),
      expectedProjectRevision: 1, confirmExternalGeneration: true,
    });
    expect(request.mock.calls[0][1]?.headers).toMatchObject({
      "X-Aether-CSRF": "1", "Idempotency-Key": "00000000-0000-4000-8000-000000000001",
    });
  });

  test("server list is the authoritative task source", async () => {
    const request = vi.fn(async () => ({
      ok: true, status: 200, json: async () => ({ items: [task] }),
    } as Response));
    const client = new GenerationApiClient("/api", request);
    expect((await client.list("project-1"))[0].taskId).toBe("server-task-1");
    expect(request).toHaveBeenCalledWith(
      "/api/projects/project-1/generation-tasks?pageSize=100", undefined,
    );
  });

  test("API errors redact token-like details", async () => {
    const request = vi.fn(async () => ({
      ok: false, status: 502,
      json: async () => ({ detail: { code: "PROVIDER_FAILED", message: "Bearer secret token leaked" } }),
    } as Response));
    const client = new GenerationApiClient("/api", request);
    await expect(client.list("project-1")).rejects.toThrow("技术详情已隐藏");
  });
});
