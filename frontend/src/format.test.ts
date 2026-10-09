import { describe, expect, it } from "vitest";
import { camelotHue, clock } from "./format";

describe("clock", () => {
  it("shows minutes and padded seconds", () => {
    expect(clock(0)).toBe("0:00");
    expect(clock(65.4)).toBe("1:05");
    expect(clock(3599)).toBe("59:59");
  });
  it("copes with nothing and negatives", () => {
    expect(clock(undefined)).toBe("0:00");
    expect(clock(-5)).toBe("0:00");
  });
});

describe("camelotHue", () => {
  it("puts the 12 numbers round the wheel", () => {
    expect(camelotHue("1A")).toBe(0);
    expect(camelotHue("4B")).toBe(90);
    expect(camelotHue("12A")).toBe(330);
  });
  it("is 0 for a key that is not there", () => {
    expect(camelotHue("")).toBe(0);
  });
});
