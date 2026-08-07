import { describe, expect, test } from "vitest";
import type { ProjectDTO } from "@aether/contracts";
import {
  createOpenCutCompatibilitySnapshot,
  formatOpenCutTimecode,
  fromOpenCutMediaTime,
  getOpenCutTicksPerSecond,
  snapToOpenCutFrame,
  toOpenCutMediaTime,
} from "../src";

describe("OpenCut compatibility adapter", () => {
  test("uses the real OpenCut WASM timing core", () => {
    expect(getOpenCutTicksPerSecond()).toBe(120_000);
    expect(toOpenCutMediaTime({ value: 3, timescale: 2 })).toBe(180_000);
    expect(fromOpenCutMediaTime(180_000)).toEqual({ value: 180_000, timescale: 120_000 });
    expect(snapToOpenCutFrame({ value: 102, timescale: 100 })).toEqual({
      value: 120_000,
      timescale: 120_000,
    });
    expect(formatOpenCutTimecode({ value: 131, timescale: 2 })).toBe("00:01:05:12");
  });

  test("maps a canonical project into an OpenCut Classic v31 snapshot", () => {
    const project: ProjectDTO = {
      id: "project-1",
      name: "Anime pilot",
      revision: 3,
      createdAt: "2026-08-07T00:00:00.000Z",
      updatedAt: "2026-08-07T01:00:00.000Z",
      materials: [{
        id: "media-1",
        name: "scene.mp4",
        type: "video",
        url: "/api/video-use/media/project-1/media-1",
        duration: { value: 5, timescale: 1 },
      }],
      timeline: {
        version: "1.1",
        tracks: [{
          id: "video-1",
          name: "Video",
          type: "video",
          clips: [{
            id: "clip-1",
            trackId: "video-1",
            materialId: "media-1",
            start: { value: 1, timescale: 1 },
            duration: { value: 2, timescale: 1 },
            sourceIn: { value: 1, timescale: 2 },
          }],
        }],
      },
    };

    const snapshot = createOpenCutCompatibilitySnapshot(project);
    expect(snapshot.generatedBy.opencutWasmVersion).toBe("0.2.10");
    expect(snapshot.project.version).toBe(31);
    expect(snapshot.project.metadata.duration).toBe(360_000);
    expect(snapshot.project.scenes[0].tracks.main.elements[0]).toMatchObject({
      id: "clip-1",
      mediaId: "media-1",
      startTime: 120_000,
      duration: 240_000,
      trimStart: 60_000,
      trimEnd: 300_000,
    });
    expect(snapshot.warnings).toEqual([]);
  });
});
