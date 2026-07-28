import { test, expect, describe } from "vitest";
import { TimelineSchema, ProjectSchema } from "../src/schemas";

describe("TimelineSchema & ProjectSchema Validation", () => {
  test("validate valid timelines and projects", () => {
    const timeline = {
      version: "1.1",
      tracks: [
        {
          id: "track-1",
          name: "Video Track 1",
          type: "video",
          clips: [
            {
              id: "clip-1",
              trackId: "track-1",
              materialId: "mat-1",
              start: { value: 0, timescale: 24 },
              duration: { value: 48, timescale: 24 },
              sourceIn: { value: 0, timescale: 24 },
            }
          ]
        }
      ]
    };

    const res = TimelineSchema.safeParse(timeline);
    expect(res.success).toBe(true);

    const project = {
      id: "project-1",
      name: "Demo Project",
      timeline: timeline,
      materials: [
        {
          id: "mat-1",
          name: "Sample.mp4",
          url: "https://example.com/sample.mp4",
          type: "video",
          duration: { value: 120, timescale: 24 }
        }
      ],
      revision: 1,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };

    const projectRes = ProjectSchema.safeParse(project);
    expect(projectRes.success).toBe(true);
  });

  test("reject invalid timelines", () => {
    const badTimeline = {
      version: "1.0", // Only 1.1 allowed
      tracks: []
    };
    const res = TimelineSchema.safeParse(badTimeline);
    expect(res.success).toBe(false);
  });
});
