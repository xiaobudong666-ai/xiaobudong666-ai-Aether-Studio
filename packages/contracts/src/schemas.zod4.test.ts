import { describe, expect, it } from "vitest";
import { AssetVersionSchema, TimelineSchema } from "./schemas";

const validAssetVersion = {
  id: "asset-version-1",
  projectId: "project-1",
  mediaId: "media-1",
  versionNo: 1,
  sha256: "a".repeat(64),
  mediaType: "video" as const,
  contentType: "video/mp4",
  sizeBytes: 128,
  probe: {
    format: { duration: "1.0", format_name: "mov,mp4" },
    streams: [{ codec_type: "video", width: 320, height: 240 }],
  },
  createdBy: "test-user",
  createdAt: "2026-10-29T00:00:00.000Z",
};

describe("AssetVersionSchema on patched Zod", () => {
  it("preserves string-keyed media probe metadata", () => {
    const parsed = AssetVersionSchema.parse(validAssetVersion);
    expect(parsed.probe).toEqual(validAssetVersion.probe);
  });

  it("rejects a non-record probe value", () => {
    const parsed = AssetVersionSchema.safeParse({
      ...validAssetVersion,
      probe: [],
    });
    expect(parsed.success).toBe(false);
  });
  it("keeps timeline output optional and normalizes legacy null", () => {
    const omitted = TimelineSchema.parse({ version: "1.1", tracks: [] });
    expect(omitted).not.toHaveProperty("output");

    const legacyNull = TimelineSchema.parse({
      version: "1.1",
      output: null,
      tracks: [],
    });
    expect(legacyNull.output).toBeUndefined();
  });

});
