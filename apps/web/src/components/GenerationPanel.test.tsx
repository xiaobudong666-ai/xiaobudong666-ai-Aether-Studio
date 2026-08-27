import { ProjectDTO } from "@aether/contracts";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { ServerGenerationTask } from "../generation";
import { GenerationPanel } from "./GenerationPanel";

const project: ProjectDTO = {
  id: "project-1", name: "受治理生成测试", revision: 1,
  timeline: { version: "1.1", tracks: [] }, materials: [],
  createdAt: "2026-08-25T00:00:00Z", updatedAt: "2026-08-25T00:00:00Z",
};

const capabilities = {
  provider: "moneyprinter", mode: "deterministic-fake", enabled: true, healthy: true,
  sourceVersion: "im12-im14-deterministic-fake-v1",
  issuedAt: "2026-08-27T00:00:00Z", expiresAt: "2026-08-27T00:05:00Z",
  videoAspects: ["16:9", "9:16", "1:1"], videoConcatModes: ["random", "sequential"],
  clipDurationSeconds: { min: 1, max: 10 }, maxOutputs: 1,
  voices: ["en-US-JennyNeural"], snapshotHash: "a".repeat(64),
  reasonCode: null, configVersionId: null, policyHash: null,
  operatorMode: "deterministic-fake",
  ownerPolicy: { published: true, enabledIntent: true },
  workerProof: { present: true, fresh: true },
  quota: {
    concurrentLimit: 4, concurrentRemaining: 4,
    monthlyRequestLimit: 100, monthlyRequestRemaining: 100,
    monthlyGeneratedSecondsLimit: 1000, monthlyGeneratedSecondsRemaining: 1000,
  },
  circuit: { state: "CLOSED" },
  killSwitch: { disabled: false, reasonCode: null },
};

const queuedTask: ServerGenerationTask = {
  taskId: "server-generation-1", projectId: "project-1", provider: "moneyprinter",
  status: "QUEUED", storedStatus: "QUEUED", progress: 0,
  message: "生成任务已进入受治理队列", attempt: 1, maxAttempts: 3,
  cancelRequested: false, rights: { allowed: false, code: "RIGHTS_MISSING" },
  results: [], createdAt: "2026-08-27T00:00:00Z", updatedAt: "2026-08-27T00:00:00Z",
};

function response(payload: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => payload } as Response;
}

function installFetch(list: ServerGenerationTask[] = []) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/generation/providers/moneyprinter/readiness")) return response(capabilities);
    if (url.includes("generation-tasks?pageSize=100")) return response({ items: list, nextCursor: null });
    if (url.endsWith("/generation-tasks/validate")) return response({ allowed: true, status: "PREFLIGHT" });
    if (url.endsWith("/generation-tasks") && init?.method === "POST") return response(queuedTask, 202);
    if (url.endsWith("/cancel")) return response({ ...queuedTask, status: "CANCELED", storedStatus: "CANCELED" });
    throw new Error(`Unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("GenerationPanel server authority", () => {
  beforeEach(() => { installFetch(); });
  afterEach(() => vi.unstubAllGlobals());

  test("IM17-46 viewer is read-only under server readiness and rights gates", async () => {
    vi.unstubAllGlobals();
    installFetch([queuedTask]);
    render(<GenerationPanel role="viewer" tenantId="tenant-1" actorId="viewer-1" project={project} assetVersions={[]} />);
    expect(await screen.findByText("server-generation-1")).toBeTruthy();
    expect(screen.getByText(/当前为只读权限/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "取消" })).toBeNull();
    expect(screen.queryByRole("button", { name: "打开生成任务" })).toBeNull();
  });

  test("preflight and submit use server APIs without a local run action", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<GenerationPanel role="owner" tenantId="tenant-1" actorId="owner-1" project={project} assetVersions={[]} />);
    expect(screen.getByLabelText("Provider 就绪状态").children).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "打开生成任务" }));
    await screen.findByText(/服务端权威状态：可创建/);
    fireEvent.change(screen.getByLabelText("生成主题"), { target: { value: "一匹马穿过雨夜城市" } });
    fireEvent.click(screen.getByLabelText("确认外部生成边界"));
    fireEvent.click(screen.getByRole("button", { name: "执行服务端预检" }));
    expect(await screen.findByText(/服务端预检通过/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "提交生成" }));
    expect(await screen.findByText(/已进入服务端队列/)).toBeTruthy();
    expect(screen.getByText("server-generation-1")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /运行本地任务/ })).toBeNull();
    const createCall = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith("/generation-tasks") && init?.method === "POST");
    expect(createCall?.[1]?.headers).toMatchObject({ "X-Aether-CSRF": "1" });
  });

  test("component rebuild restores tasks from server rather than localStorage", async () => {
    vi.unstubAllGlobals();
    const fetchMock = installFetch([queuedTask]);
    const props = { role: "owner" as const, tenantId: "tenant-1", actorId: "owner-1", project, assetVersions: [] };
    const first = render(<GenerationPanel {...props} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/projects/project-1/generation-tasks?pageSize=100", undefined));
    first.unmount();
    render(<GenerationPanel {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "打开生成任务" }));
    expect(await screen.findByText("server-generation-1")).toBeTruthy();
    expect(screen.getByText(/排队中/)).toBeTruthy();
  });

  test("IM17-45 late readiness and task responses cannot pollute a new project", async () => {
    let resolveOld: ((value: Response) => void) | undefined;
    const oldList = new Promise<Response>((resolve) => { resolveOld = resolve; });
    const nextProject = { ...project, id: "project-2", name: "Second" };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("readiness")) return response(capabilities);
      if (url.includes("project-1/generation-tasks")) return oldList;
      if (url.includes("project-2/generation-tasks")) return response({ items: [{ ...queuedTask, taskId: "server-generation-2", projectId: "project-2" }] });
      throw new Error(url);
    }));
    const rendered = render(<GenerationPanel role="owner" tenantId="tenant-1" actorId="owner-1" project={project} assetVersions={[]} />);
    rendered.rerender(<GenerationPanel role="owner" tenantId="tenant-1" actorId="owner-1" project={nextProject} assetVersions={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "打开生成任务" }));
    expect(await screen.findByText("server-generation-2")).toBeTruthy();
    resolveOld?.(response({ items: [queuedTask] }));
    await waitFor(() => expect(screen.queryByText("server-generation-1")).toBeNull());
  });

  test("rights-allowed result creates only an adopted=false editor reference", async () => {
    vi.unstubAllGlobals();
    installFetch([{
      ...queuedTask,
      status: "SUCCEEDED",
      storedStatus: "RIGHTS_BLOCKED",
      progress: 100,
      rights: { allowed: true, code: "RIGHTS_ALLOWED" },
      results: [{
        assetVersionId: "asset-version-1", mediaId: "media-1",
        checksum: "b".repeat(64), contentType: "video/mp4", sizeBytes: 5,
        provenance: { generationTaskId: "server-generation-1" },
        rights: { allowed: true, code: "RIGHTS_ALLOWED" },
      }],
    }]);
    render(<GenerationPanel role="owner" tenantId="tenant-1" actorId="owner-1" project={project} assetVersions={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "打开生成任务" }));
    const useButton = await screen.findByRole("button", { name: "用于快速制作" });
    expect((useButton as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(useButton);
    expect(await screen.findByText(/adopted=false 的剪辑引用/)).toBeTruthy();
    expect(screen.getByText(/全部 adopted=false，未写入最终时间线/)).toBeTruthy();
  });
});
