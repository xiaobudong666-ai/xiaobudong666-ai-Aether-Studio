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
  name: "Launch Trailer",
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
    vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
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

describe("App Workbench baseline", () => {
  test("renders all workbench regions and uses same-origin API/SSE paths", async () => {
    render(<App />);

    expect(screen.getByText("Library & Materials")).toBeTruthy();
    expect(screen.getByText("Canvas Monitor (480p Proxy Target)")).toBeTruthy();
    expect(screen.getByText("Property Inspector & Tasks")).toBeTruthy();
    expect(screen.getByText(/Timeline tracks/i)).toBeTruthy();

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith("/api/projects");
      expect(MockEventSource.urls).toContain("/api/events");
    });
  });

  test("creates a project through the proxied API", async () => {
    render(<App />);

    fireEvent.change(screen.getByPlaceholderText("New project name"), {
      target: { value: createdProject.name },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create Project" }));

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Launch Trailer (r1)" }))
        .toBeTruthy();
    });

    expect(fetch).toHaveBeenCalledWith(
      "/api/projects",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
