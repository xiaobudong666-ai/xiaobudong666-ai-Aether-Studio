import { test, expect, describe } from "vitest";
import { RationalTime } from "../src/index";

describe("RationalTime Timing & Precision Tests", () => {
  test("creation and basic math conversions", () => {
    const t = new RationalTime(24, 24);
    expect(t.toSeconds()).toBe(1);
    expect(t.toMilliseconds()).toBe(1000);

    const t2 = RationalTime.fromSeconds(1.5, 24000);
    expect(t2.value).toBe(36000);
    expect(t2.timescale).toBe(24000);
  });

  test("high frame rate precision and non-integer timescales", () => {
    // 23.976 fps -> NTSC timescale is 24000/1001
    const timescale23976 = 24000; // standard timescale mapping
    const frameDuration23976 = new RationalTime(1001, timescale23976);
    expect(frameDuration23976.toSeconds()).toBeCloseTo(0.041708, 6);

    // Accumulate 24 frames of 23.976
    let accumulated = new RationalTime(0, timescale23976);
    for (let i = 0; i < 24; i++) {
      accumulated = accumulated.add(frameDuration23976);
    }
    // Should be exactly 24024 / 24000 = 1.001 seconds
    expect(accumulated.toSeconds()).toBe(1.001);

    // 29.97 fps -> NTSC timescale 30000/1001
    const timescale2997 = 30000;
    const frameDuration2997 = new RationalTime(1001, timescale2997);
    expect(frameDuration2997.toSeconds()).toBeCloseTo(0.033367, 6);

    // 59.94 fps -> NTSC timescale 60000/1001
    const timescale5994 = 60000;
    const frameDuration5994 = new RationalTime(1001, timescale5994);
    expect(frameDuration5994.toSeconds()).toBeCloseTo(0.016683, 6);
  });

  test("long-duration timing and float safety stability", () => {
    const timescale = 24000;
    // 100 hours of 24fps video
    // 100 * 3600 seconds = 360,000 seconds
    const totalSeconds = 100 * 3600;
    const longTime = RationalTime.fromSeconds(totalSeconds, timescale);

    expect(longTime.value).toBe(8640000000);
    expect(Number.isSafeInteger(longTime.value)).toBe(true);
    expect(longTime.toSeconds()).toBe(totalSeconds);

    // Verify addition on long time retains exact frame correctness
    const oneFrame = new RationalTime(1000, timescale); // 1/24 seconds
    const nextFrame = longTime.add(oneFrame);
    expect(nextFrame.value).toBe(8640001000);
    expect(nextFrame.subtract(longTime).equals(oneFrame)).toBe(true);
  });

  test("safety boundaries and range checks", () => {
    // Zero or negative timescale must fail
    expect(() => new RationalTime(10, 0)).toThrow();
    expect(() => new RationalTime(10, -24)).toThrow();

    // Floating values must fail
    expect(() => new RationalTime(10.5, 24)).toThrow();
    expect(() => new RationalTime(10, 24.5)).toThrow();

    // Max safe integer boundaries check
    const maxSafe = Number.MAX_SAFE_INTEGER;
    expect(() => new RationalTime(maxSafe, 24000)).not.toThrow();
    expect(() => new RationalTime(maxSafe + 1, 24000)).toThrow();
  });
});
