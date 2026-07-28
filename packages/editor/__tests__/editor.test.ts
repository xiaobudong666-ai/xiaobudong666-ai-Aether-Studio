import { test, expect, describe, vi } from "vitest";
import { BaseEditorAdapter, IMaterialLoader, ICanvasAdapter, ITimelineController } from "../src/index";
import { RationalTime } from "@aether/contracts";

describe("Editor Abstract Interface Validation", () => {
  test("implements BaseEditorAdapter correctly", async () => {
    const mockLoader: IMaterialLoader = {
      loadMaterial: vi.fn(),
      unloadMaterial: vi.fn(),
      getLoadedMaterials: vi.fn().mockReturnValue(["mat-1"]),
    };

    const mockCanvas: ICanvasAdapter = {
      attachCanvas: vi.fn(),
      detachCanvas: vi.fn(),
      resize: vi.fn(),
      setCurrentTime: vi.fn(),
      getCurrentTime: vi.fn().mockReturnValue(new RationalTime(0, 24)),
      play: vi.fn(),
      pause: vi.fn(),
      isPlaying: vi.fn().mockReturnValue(false),
      renderFrame: vi.fn(),
    };

    const mockTimeline: ITimelineController = {
      loadTimeline: vi.fn(),
      getTimeline: vi.fn(),
      insertClip: vi.fn(),
      removeClip: vi.fn(),
      updateClipPosition: vi.fn(),
      snapTime: vi.fn().mockImplementation((t) => t),
      zoom: vi.fn(),
    };

    const adapter = new BaseEditorAdapter(mockLoader, mockCanvas, mockTimeline);
    await adapter.initialize();

    expect(adapter.loader.getLoadedMaterials()).toContain("mat-1");
    expect(adapter.canvas.isPlaying()).toBe(false);
    expect(adapter.timeline.snapTime(new RationalTime(10, 24), 100).value).toBe(10);

    await adapter.destroy();
    expect(mockCanvas.detachCanvas).toHaveBeenCalled();
  });
});
