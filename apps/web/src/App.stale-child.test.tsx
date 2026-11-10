import { beforeEach, describe, expect, test, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "./App";

type TestProject = {
  id: string;
  name: string;
  timeline: { version: string; tracks: [] };
  materials: [];
  revision: number;
  createdAt: string;
  updatedAt: string;
};

const { priorCallbacks } = vi.hoisted(() => ({
  priorCallbacks: [] as Array<(project: TestProject) => void>,
}));

vi.mock("./components/GenerationPanel", () => ({
  GenerationPanel: ({
    project, onProjectUpdated,
  }: { project: TestProject | null; onProjectUpdated: (project: TestProject) => void }) => {
    // Capture the async callback from project 1 before the selection changes.
    if (project?.id === "project-1") priorCallbacks.push(onProjectUpdated);
    return null;
  },
}));

class FakeEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) { void url; }
  addEventListener(name: string, listener: (event: MessageEvent) => void) {
    void name;
    void listener;
  }
  close() {}
}

const first: TestProject = {
  id: "project-1", name: "私有项目一",
  timeline: { version: "1.1", tracks: [] }, materials: [], revision: 1,
  createdAt: "2026-11-11T00:00:00Z", updatedAt: "2026-11-11T00:00:00Z",
};
const second: TestProject = { ...first, id: "project-2", name: "项目二" };

beforeEach(() => {
  priorCallbacks.length = 0;
  vi.stubGlobal("EventSource", FakeEventSource);
});

describe("G72 stale GenerationPanel callback workspace fence", () => {
  test("ignores a previous project's delayed callback before and after the next detail GET", async () => {
    let resolveSecond!: (value: Response) => void;
    const pendingSecond = new Promise<Response>((resolve) => { resolveSecond = resolve; });
    const ok = (value: unknown) => ({ ok: true, status: 200, json: async () => value } as Response);
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) {
        return ok({
          id: "owner-1", email: "owner@example.com", displayName: "Owner", role: "owner",
          tenant: { id: "tenant-1", name: "Test", slug: "test" },
          quotas: {
            projects: 50, storageBytes: 1000, storageBytesUsed: 0,
            concurrentRenders: 2, monthlyRenderSeconds: 1000,
            monthlyRenderSecondsUsed: 0, period: "2026-11",
          },
        });
      }
      if (url.endsWith("/projects")) return ok([first, second]);
      if (url.endsWith("/projects/project-1")) return ok(first);
      if (url.endsWith("/projects/project-2")) return pendingSecond;
      if (url.endsWith("/asset-versions")) return ok([]);
      return ok([]);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    expect(await screen.findByRole("option", { name: "私有项目一（版本 1）" })).toBeTruthy();
    await waitFor(() => expect(priorCallbacks.length).toBeGreaterThan(0));
    const staleCallback = priorCallbacks[priorCallbacks.length - 1];

    fireEvent.change(screen.getByLabelText("选择项目"), { target: { value: "project-2" } });
    expect((screen.getByRole("button", { name: "导出 OpenCut 快照" }) as HTMLButtonElement).disabled).toBe(true);

    await act(async () => { staleCallback({ ...first, name: "不应回显的旧项目内容" }); });
    expect((screen.getByLabelText("选择项目") as HTMLSelectElement).value).toBe("project-2");
    expect((screen.getByRole("button", { name: "导出 OpenCut 快照" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByText(/不应回显的旧项目内容/)).toBeNull();

    await act(async () => { resolveSecond(ok(second)); });
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "导出 OpenCut 快照" }) as HTMLButtonElement).disabled).toBe(false)
    );
    await act(async () => { staleCallback({ ...first, name: "过期异步结果" }); });
    expect(screen.queryByText(/过期异步结果/)).toBeNull();
    expect((screen.getByLabelText("选择项目") as HTMLSelectElement).value).toBe("project-2");
    expect((screen.getByRole("button", { name: "导出 OpenCut 快照" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
