import { beforeEach, describe, expect, test, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "./App";
import { AssetLibrary } from "./components/AssetLibrary";

class MockEventSource {
  static urls: string[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners: Record<string, ((event: MessageEvent) => void)[]> = {};

  constructor(public url: string) {
    MockEventSource.urls.push(url);
    queueMicrotask(() => this.onopen?.());
  }

  addEventListener(
    type: string,
    listener: (event: MessageEvent) => void,
  ) {
    this.listeners[type] = this.listeners[type] || [];
    this.listeners[type].push(listener);
  }

  close() {}
}

const createdProject = {
  id: "project-1",
  name: "发布预告片",
  timeline: { version: "1.1", tracks: [] },
  materials: [],
  revision: 1,
  createdAt: "2026-07-30T00:00:00Z",
  updatedAt: "2026-07-30T00:00:00Z",
};

beforeEach(() => {
  MockEventSource.urls = [];
  vi.stubGlobal("EventSource", MockEventSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            id: "owner-1", email: "owner@example.com", displayName: "Owner", role: "owner",
            tenant: { id: "tenant-1", name: "Aether Test", slug: "aether-test" },
            quotas: {
              projects: 50, storageBytes: 1000, storageBytesUsed: 0,
              concurrentRenders: 2, monthlyRenderSeconds: 1000,
              monthlyRenderSecondsUsed: 0, period: "2026-08",
            },
          }),
        } as Response;
      }
      if (init?.method === "POST") {
        return {
          ok: true,
          status: 201,
          json: async () => createdProject,
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        json: async () => [],
      } as Response;
    }),
  );
});

describe("Aether Studio 中文工作台", () => {
  test("renders all workbench regions and uses same-origin API/SSE paths", async () => {
    render(<App />);

    expect(await screen.findByText("素材库")).toBeTruthy();
    expect(screen.getByText("画面监看 · 480p 代理目标")).toBeTruthy();
    expect(screen.getByText("属性与任务")).toBeTruthy();
    expect(screen.getByText(/时间线轨道/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "快速制作短视频" })).toBeTruthy();
    expect(screen.getByText(/不会自动采纳或发布/)).toBeTruthy();
    expect(screen.getByText(/项目 0\/50/)).toBeTruthy();

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith("/api/projects");
      expect(MockEventSource.urls).toContain("/api/events");
    });
  });

  test("creates a project through the proxied API", async () => {
    render(<App />);

    fireEvent.change(await screen.findByPlaceholderText("输入新项目名称"), {
      target: { value: createdProject.name },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建项目" }));

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "发布预告片（版本 1）" }))
        .toBeTruthy();
    });

    expect(screen.getByRole("status").textContent).toContain("已创建");

    expect(fetch).toHaveBeenCalledWith(
      "/api/projects",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "X-Aether-CSRF": "1" }),
      }),
    );
  });

  test("completes governed asset, canonical task, adoption, and master operations", async () => {
    const projectWithMaterial = {
      ...createdProject,
      materials: [{
        id: "media-1",
        name: "source.mp4",
        url: "/api/media/media-1",
        type: "video",
        contentType: "video/mp4",
        duration: { value: 24, timescale: 24 },
        sizeBytes: 42,
      }],
    };
    const assetVersion = {
      id: "asset-version-1",
      projectId: createdProject.id,
      mediaId: "media-1",
      versionNo: 1,
      sha256: "a".repeat(64),
      mediaType: "video",
      contentType: "video/mp4",
      sizeBytes: 42,
      probe: {
        durationSeconds: 1,
        video: { codec: "h264", width: 320, height: 240 },
        audio: { codec: "aac", sampleRate: 48000 },
      },
      createdBy: "owner-1",
      createdAt: "2026-08-18T00:00:00Z",
    };
    const candidate = {
      id: "candidate-1",
      projectId: createdProject.id,
      taskId: "task-1",
      artifactRef: "/api/renders/task-1/artifact",
      inputRevision: 3,
      status: "READY" as const,
      createdAt: "2026-08-18T00:02:00Z",
    };
    const master = {
      id: "master-1",
      projectId: createdProject.id,
      revisionNo: 1,
      artifactRef: "/api/renders/task-1/artifact",
      sha256: null,
      createdAt: "2026-08-18T00:03:00Z",
      adoption: {
        id: "adoption-1",
        candidateId: candidate.id,
        adoptedBy: "owner-1",
        adoptedAt: "2026-08-18T00:03:00Z",
        reason: "导演确认最终版本",
        supersedesId: null,
      },
    };
    let rightsAllowed = false;
    let adoptionAttempts = 0;
    let adopted = false;
    const adoptionKeys: string[] = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              id: "owner-1", email: "owner@example.com", displayName: "Owner", role: "owner",
              tenant: { id: "tenant-1", name: "Aether Test", slug: "aether-test" },
              quotas: {
                projects: 50, storageBytes: 1000, storageBytesUsed: 42,
                concurrentRenders: 2, monthlyRenderSeconds: 1000,
                monthlyRenderSecondsUsed: 1, period: "2026-08",
              },
            }),
          } as Response;
        }
        if (url === "/api/projects") {
          return { ok: true, status: 200, json: async () => [projectWithMaterial] } as Response;
        }
        if (url === `/api/projects/${createdProject.id}`) {
          return { ok: true, status: 200, json: async () => projectWithMaterial } as Response;
        }
        if (url === `/api/projects/${createdProject.id}/asset-versions`) {
          return { ok: true, status: 200, json: async () => [assetVersion] } as Response;
        }
        if (url.includes(`/asset-versions/${assetVersion.id}/rights-check`)) {
          return {
            ok: true,
            status: 200,
            json: async () => rightsAllowed ? {
              assetVersionId: assetVersion.id,
              allowed: true,
              code: "RIGHTS_ALLOWED",
              snapshot: {
                id: "rights-1",
                assetVersionId: assetVersion.id,
                status: "ALLOWED",
                purpose: "EXPORT",
                territory: "GLOBAL",
                validFrom: null,
                validUntil: null,
                evidenceRef: "evidence://owner-approved",
                capturedBy: "owner-1",
                capturedAt: "2026-08-18T00:01:00Z",
              },
            } : {
              assetVersionId: assetVersion.id,
              allowed: false,
              code: "RIGHTS_MISSING",
              snapshot: null,
            },
          } as Response;
        }
        if (url.endsWith(`/asset-versions/${assetVersion.id}/rights-snapshots`) && init?.method === "POST") {
          rightsAllowed = true;
          return { ok: true, status: 201, json: async () => ({ id: "rights-1" }) } as Response;
        }
        if (url.includes("/render-tasks?")) {
          return {
            ok: true,
            status: 200,
            json: async () => [{
              taskId: "task-1",
              projectId: createdProject.id,
              progress: 5,
              status: "failed",
              canonicalStatus: "FAILED",
              message: "旧状态",
              updatedAt: "2026-08-18T00:00:00Z",
            }, {
              taskId: "task-1",
              projectId: createdProject.id,
              progress: 100,
              status: "failed",
              canonicalStatus: "SUCCEEDED",
              message: "规范状态确认完成",
              artifactUrl: "/api/renders/task-1/artifact",
              attempts: 2,
              updatedAt: "2026-08-18T00:02:00Z",
            }, {
              taskId: "task-unknown",
              projectId: createdProject.id,
              progress: 100,
              status: "completed",
              canonicalStatus: "UNKNOWN",
              message: "服务端状态需要重新确认",
              artifactUrl: "/api/renders/task-unknown/artifact",
              updatedAt: "2026-08-18T00:03:00Z",
            }],
          } as Response;
        }
        if (url.endsWith(`/projects/${createdProject.id}/candidates`)) {
          return {
            ok: true,
            status: 200,
            json: async () => [{ ...candidate, status: adopted ? "ADOPTED" : "READY" }],
          } as Response;
        }
        if (url.endsWith(`/projects/${createdProject.id}/masters`)) {
          return { ok: true, status: 200, json: async () => adopted ? [master] : [] } as Response;
        }
        if (url.endsWith(`/candidates/${candidate.id}/adopt`) && init?.method === "POST") {
          const headers = init.headers as Record<string, string>;
          adoptionKeys.push(headers["Idempotency-Key"]);
          adoptionAttempts += 1;
          if (adoptionAttempts === 1) throw new Error("network interrupted");
          adopted = true;
          return { ok: true, status: 201, json: async () => master } as Response;
        }
        return { ok: true, status: 200, json: async () => [] } as Response;
      }),
    );

    render(<App />);

    expect(await screen.findByText("source.mp4", { exact: true })).toBeTruthy();
    fireEvent.click(screen.getByText("素材治理 · v1"));
    expect(await screen.findByText("缺少权利记录")).toBeTruthy();
    expect(screen.getByTitle(assetVersion.sha256).textContent).toBe("aaaaaaaaaaaa");

    fireEvent.click(screen.getByRole("button", { name: "记录权利快照" }));
    fireEvent.change(screen.getByLabelText("生效时间"), {
      target: { value: "2026-08-20T10:00" },
    });
    fireEvent.change(screen.getByLabelText("结束时间"), {
      target: { value: "2026-08-20T09:00" },
    });
    fireEvent.click(screen.getByLabelText(/确认追加新的不可变快照/));
    fireEvent.click(screen.getByRole("button", { name: "确认追加快照" }));
    expect(await screen.findByText("有效期结束时间必须晚于开始时间。")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("生效时间"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("结束时间"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "确认追加快照" }));
    expect(await screen.findByText("允许导出")).toBeTruthy();

    expect(screen.getAllByText("已完成").length).toBeGreaterThan(0);
    expect(screen.getByText("状态待确认")).toBeTruthy();
    expect(screen.getByRole("button", { name: "重新查询状态" })).toBeTruthy();
    expect(screen.getAllByText(/规范状态确认完成/).length).toBe(1);
    expect(screen.getAllByRole("link", { name: "下载 MP4 成片" })).toHaveLength(1);

    fireEvent.click(await screen.findByRole("button", { name: "采纳为母版" }));
    fireEvent.change(screen.getByLabelText("采纳原因"), {
      target: { value: "导演确认最终版本" },
    });
    fireEvent.click(screen.getByLabelText(/确认这是一次明确采纳/));
    fireEvent.click(screen.getByRole("button", { name: "确认采纳" }));
    expect(await screen.findByText(/本次操作标识已保留/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认采纳" }));

    expect(await screen.findByText("母版 v1")).toBeTruthy();
    expect(screen.getByRole("link", { name: "下载母版" })).toBeTruthy();
    expect(adoptionKeys).toHaveLength(2);
    expect(adoptionKeys[0]).toBe(adoptionKeys[1]);
  });

  test("keeps governance and timeline writes disabled for viewers", async () => {
    const material = {
      id: "media-viewer",
      name: "viewer-source.mp4",
      url: "/api/media/media-viewer",
      type: "video" as const,
      contentType: "video/mp4",
      duration: { value: 24, timescale: 24 },
      sizeBytes: 42,
    };
    const assetVersion = {
      id: "asset-viewer",
      projectId: "project-viewer",
      mediaId: material.id,
      versionNo: 1,
      sha256: "b".repeat(64),
      mediaType: "video" as const,
      contentType: "video/mp4",
      sizeBytes: 42,
      probe: {},
      createdBy: "owner-1",
      createdAt: "2026-08-18T00:00:00Z",
    };
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        assetVersionId: assetVersion.id,
        allowed: false,
        code: "RIGHTS_MISSING",
        snapshot: null,
      }),
    } as Response)));

    render(
      <AssetLibrary
        materials={[material]}
        onUploadMaterial={vi.fn(async () => undefined)}
        onAddClipToTimeline={vi.fn(async () => undefined)}
        canEdit={false}
        hasProject
        projectId="project-viewer"
        assetVersions={[assetVersion]}
        apiBase="/api"
        onSessionExpired={vi.fn()}
      />,
    );

    expect((screen.getByRole("button", { name: "+ 添加到时间线" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByText("素材治理 · v1"));
    expect(await screen.findByText("缺少权利记录")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "记录权利快照" })).toBeNull();
    expect(screen.getByText(/只读成员可以查看/)).toBeTruthy();
  });
});
