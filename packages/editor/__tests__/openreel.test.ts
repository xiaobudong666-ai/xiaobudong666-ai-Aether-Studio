import { describe, expect, test } from "vitest";
import type { ProjectDTO } from "@aether/contracts";
import {
  createOpenReelProjectFile,
  OPENREEL_COMMIT,
  OPENREEL_SCHEMA_VERSION,
} from "../src";

describe("OpenReel compatibility adapter", () => {
  test("creates an importable OpenReel 1.0.0 project file", () => {
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

    const file = createOpenReelProjectFile(project);
    expect(OPENREEL_COMMIT).toBe("8459024d4c82ee16a2e14537553884a623ae9c4e");
    expect(file.version).toBe(OPENREEL_SCHEMA_VERSION);
    expect(file.project.timeline.duration).toBe(3);
    expect(file.project.timeline.tracks[0].clips[0]).toMatchObject({
      id: "clip-1",
      mediaId: "media-1",
      startTime: 1,
      duration: 2,
      inPoint: 0.5,
      outPoint: 2.5,
    });
    expect(file.project.mediaLibrary.items[0]).toMatchObject({
      id: "media-1",
      blob: null,
      fileHandle: null,
      isPlaceholder: true,
      originalUrl: "/api/video-use/media/project-1/media-1",
    });
  });
});
