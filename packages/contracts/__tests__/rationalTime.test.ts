import { test, expect, describe } from "vitest";
import { RationalTime } from "../src/index";

describe("RationalTime", () => {
  test("creation and conversion to/from seconds/ms", () => {
    const t = new RationalTime(24, 24);
    expect(t.toSeconds()).toBe(1);
    expect(t.toMilliseconds()).toBe(1000);

    const t2 = RationalTime.fromSeconds(1.5, 24);
    expect(t2.value).toBe(36);
    expect(t2.timescale).toBe(24);

    const t3 = RationalTime.fromMilliseconds(500, 1000);
    expect(t3.value).toBe(500);
    expect(t3.toSeconds()).toBe(0.5);
  });

  test("simplification", () => {
    const t = new RationalTime(100, 200);
    const s = t.simplify();
    expect(s.value).toBe(1);
    expect(s.timescale).toBe(2);
  });

  test("addition and subtraction with same timescale", () => {
    const t1 = new RationalTime(10, 24);
    const t2 = new RationalTime(5, 24);

    const add = t1.add(t2);
    expect(add.value).toBe(15);
    expect(add.timescale).toBe(24);

    const sub = t1.subtract(t2);
    expect(sub.value).toBe(5);
    expect(sub.timescale).toBe(24);
  });

  test("addition and subtraction with different timescales", () => {
    const t1 = new RationalTime(1, 2);
    const t2 = new RationalTime(1, 3);

    const add = t1.add(t2);
    expect(add.value).toBe(5);
    expect(add.timescale).toBe(6);

    const sub = t1.subtract(t2);
    expect(sub.value).toBe(1);
    expect(sub.timescale).toBe(6);
  });

  test("comparisons", () => {
    const t1 = new RationalTime(1, 2);
    const t2 = new RationalTime(2, 4);
    const t3 = new RationalTime(3, 4);

    expect(t1.equals(t2)).toBe(true);
    expect(t1.equals(t3)).toBe(false);
    expect(t3.greaterThan(t1)).toBe(true);
    expect(t1.lessThan(t3)).toBe(true);
  });
});
