import { test, expect, describe } from "vitest";
import {
  AssetVersionSchema,
  CandidateSchema,
  ProjectSchema,
  RightsSnapshotSchema,
  TimelineSchema,
  canonicalTaskStatus,
} from "../src/schemas";

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

  test("rejects fractional and unsafe rational time values", () => {
    expect(
      TimelineSchema.safeParse({
        version: "1.1",
        tracks: [{
          id: "track-1",
          name: "Video",
          type: "video",
          clips: [{
            id: "clip-1",
            trackId: "track-1",
            materialId: "mat-1",
            start: { value: 0.5, timescale: 24 },
            duration: { value: 24, timescale: 24 },
            sourceIn: { value: 0, timescale: 24 },
          }],
        }],
      }).success
    ).toBe(false);
  });

  test("normalizes legacy task aliases into canonical states", () => {
    expect(canonicalTaskStatus("queued")).toBe("QUEUED");
    expect(canonicalTaskStatus("dispatching")).toBe("RUNNING");
    expect(canonicalTaskStatus("completed")).toBe("SUCCEEDED");
    expect(canonicalTaskStatus("PARTIAL")).toBe("PARTIAL");
    expect(() => canonicalTaskStatus("invented")).toThrow();
  });

  test("validates typed asset, rights and candidate contracts", () => {
    expect(AssetVersionSchema.safeParse({
      id: "asset-1",
      projectId: "project-1",
      mediaId: "media-1",
      versionNo: 1,
      sha256: "a".repeat(64),
      mediaType: "video",
      contentType: "video/mp4",
      sizeBytes: 42,
      probe: { durationSeconds: 1 },
      createdBy: "user-1",
      createdAt: new Date().toISOString(),
    }).success).toBe(true);
    expect(RightsSnapshotSchema.safeParse({
      id: "rights-1",
      assetVersionId: "asset-1",
      status: "ALLOWED",
      purpose: "EXPORT",
      territory: "GLOBAL",
      validFrom: null,
      validUntil: null,
      evidenceRef: "evidence://owner-approved",
      capturedBy: "user-1",
      capturedAt: new Date().toISOString(),
    }).success).toBe(true);
    expect(CandidateSchema.safeParse({
      id: "candidate-1",
      projectId: "project-1",
      taskId: "task-1",
      artifactRef: "/api/renders/task-1/artifact",
      inputRevision: 3,
      status: "READY",
      createdAt: new Date().toISOString(),
    }).success).toBe(true);
  });
});
