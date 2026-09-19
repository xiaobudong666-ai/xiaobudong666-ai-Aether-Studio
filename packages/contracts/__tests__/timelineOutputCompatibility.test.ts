import { describe, expect, test } from "vitest";
import { TimelineSchema } from "../src/schemas";

describe("Timeline v1.1 output compatibility", () => {
  test("accepts legacy API null output and normalizes it to undefined", () => {
    const parsed = TimelineSchema.parse({
      version: "1.1",
      output: null,
      tracks: [],
    });

    expect(parsed.output).toBeUndefined();
  });

  test("preserves explicit vertical output", () => {
    const parsed = TimelineSchema.parse({
      version: "1.1",
      output: { aspect: "9:16", width: 1080, height: 1920 },
      tracks: [],
    });

    expect(parsed.output).toEqual({ aspect: "9:16", width: 1080, height: 1920 });
  });
});
