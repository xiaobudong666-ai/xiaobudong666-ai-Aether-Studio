import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { FinishedMediaPanel } from "./FinishedMediaPanel";
import { PropertyInspector } from "./PropertyInspector";

class MockEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public url: string) {}

  addEventListener() {}

  close() {}
}

function jsonResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("project switch isolation", () => {
  test("ignores late candidate and master responses from the previous project", async () => {
    let resolveOldCandidates!: (response: Response) => void;
    let resolveOldMasters!: (response: Response) => void;
    const oldCandidates = new Promise<Response>((resolve) => {
      resolveOldCandidates = resolve;
    });
    const oldMasters = new Promise<Response>((resolve) => {
      resolveOldMasters = resolve;
    });
    const newCandidate = {
      id: "new-candidate-1",
      projectId: "project-new",
      taskId: "task-new",
      artifactRef: "/new.mp4",
      inputRevision: 1,
      status: "READY",
      createdAt: "2026-08-19T00:00:00Z",
    };
    const oldCandidate = {
      ...newCandidate,
      id: "old-candidate-1",
      projectId: "project-old",
      taskId: "task-old",
      artifactRef: "/old.mp4",
    };

    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/projects/project-old/candidates") return oldCandidates;
      if (url === "/api/projects/project-old/masters") return oldMasters;
      if (url === "/api/projects/project-new/candidates") {
        return Promise.resolve(jsonResponse([newCandidate]));
      }
      if (url === "/api/projects/project-new/masters") {
        return Promise.resolve(jsonResponse([]));
      }
      return Promise.resolve(jsonResponse([]));
    });
    vi.stubGlobal("fetch", fetchMock);

    const props = {
      apiBase: "/api",
      canEdit: true,
      onSessionExpired: vi.fn(),
      refreshToken: "",
    };
    const { rerender } = render(
      <FinishedMediaPanel {...props} projectId="project-old" />,
    );

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/projects/project-old/candidates");
    });

    rerender(<FinishedMediaPanel {...props} projectId="project-new" />);
    expect(await screen.findByText("候选 new-cand")).toBeTruthy();

    await act(async () => {
      resolveOldCandidates(jsonResponse([oldCandidate]));
      resolveOldMasters(jsonResponse([]));
      await Promise.resolve();
    });

    expect(screen.queryByText("候选 old-cand")).toBeNull();
    expect(screen.getByText("候选 new-cand")).toBeTruthy();
  });

  test("ignores a late task-history response from the previous project", async () => {
    vi.stubGlobal("EventSource", MockEventSource);
    let resolveOldTasks!: (response: Response) => void;
    const oldTasks = new Promise<Response>((resolve) => {
      resolveOldTasks = resolve;
    });
    const newTask = {
      taskId: "new-task-123",
      projectId: "project-new",
      progress: 20,
      status: "processing",
      canonicalStatus: "RUNNING",
      message: "new",
      updatedAt: "2026-08-19T00:00:00Z",
    };
    const oldTask = {
      ...newTask,
      taskId: "old-task-123",
      projectId: "project-old",
      message: "old",
    };

    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/render-tasks?projectId=project-old") return oldTasks;
      if (url === "/api/render-tasks?projectId=project-new") {
        return Promise.resolve(jsonResponse([newTask]));
      }
      if (url.endsWith("/candidates") || url.endsWith("/masters")) {
        return Promise.resolve(jsonResponse([]));
      }
      return Promise.resolve(jsonResponse([]));
    });
    vi.stubGlobal("fetch", fetchMock);

    const props = {
      selectedClip: null,
      onTriggerRender: vi.fn(async () => {}),
      apiBase: "/api",
      canEdit: true,
      canRender: true,
      onSessionExpired: vi.fn(),
    };
    const { rerender } = render(
      <PropertyInspector {...props} projectId="project-old" />,
    );

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/render-tasks?projectId=project-old");
    });

    rerender(<PropertyInspector {...props} projectId="project-new" />);
    expect(await screen.findByText("任务：new-task")).toBeTruthy();

    await act(async () => {
      resolveOldTasks(jsonResponse([oldTask]));
      await Promise.resolve();
    });

    expect(screen.queryByText("任务：old-task")).toBeNull();
    expect(screen.getByText("任务：new-task")).toBeTruthy();
  });
});
