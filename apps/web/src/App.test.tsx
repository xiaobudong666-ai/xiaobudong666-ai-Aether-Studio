import { beforeEach, describe, expect, test, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "./App";

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
});
