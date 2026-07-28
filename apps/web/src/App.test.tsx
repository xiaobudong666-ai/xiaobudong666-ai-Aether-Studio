import { describe, test, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "./App";

// Mock EventSource for testing PropertyInspector without actual browser backend SSE link
class MockEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners: Record<string, ((e: any) => void)[]> = {};

  constructor(public url: string) {
    // Automatically trigger connection
    setTimeout(() => {
      if (this.onopen) this.onopen();
    }, 50);
  }

  addEventListener(type: string, listener: (e: any) => void) {
    this.listeners[type] = this.listeners[type] || [];
    this.listeners[type].push(listener);
  }

  close() {}
}

vi.stubGlobal("EventSource", MockEventSource);

describe("App Workbench baseline", () => {
  test("renders all three workbench columns and bottom timeline", () => {
    render(<App />);

    // Check Left Panel (Library)
    expect(screen.getByText("Library & Materials")).toBeInTheDocument();

    // Check Middle Panel (Canvas)
    expect(screen.getByText("Canvas Monitor (480p Proxy Target)")).toBeInTheDocument();

    // Check Right Panel (Property Inspector / Tasks)
    expect(screen.getByText("Property Inspector & Tasks")).toBeInTheDocument();

    // Check Bottom Timeline (RationalTime segments)
    expect(screen.getByText(/Timeline tracks/i)).toBeInTheDocument();
  });
});
