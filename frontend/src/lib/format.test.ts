import { afterEach, describe, expect, it, vi } from "vitest";
import { ago, checkedIn, count, names, nowSeconds, onDay, when } from "./format";

const local = (y: number, m: number, d: number, h: number, min: number) => new Date(y, m - 1, d, h, min).getTime() / 1000;

afterEach(() => vi.useRealTimers());

describe("ago", () => {
  it("says never for no time, and just now under a minute", () => {
    expect(ago(null, 100)).toBe("never");
    expect(ago(70, 100)).toBe("just now");
    expect(ago(200, 100)).toBe("just now");
  });
  it("counts minutes, hours and days, singular and plural", () => {
    expect(ago(100 - 60, 100)).toBe("1 minute ago");
    expect(ago(0, 8 * 60)).toBe("8 minutes ago");
    expect(ago(0, 3600)).toBe("1 hour ago");
    expect(ago(0, 5 * 3600)).toBe("5 hours ago");
    expect(ago(0, 86_400)).toBe("1 day ago");
    expect(ago(0, 3 * 86_400)).toBe("3 days ago");
  });
});

describe("checkedIn", () => {
  it("reads as a phrase", () => {
    expect(checkedIn(null, 10)).toBe("never checked in");
    expect(checkedIn(0, 600)).toBe("checked in 10 minutes ago");
  });
});

describe("when", () => {
  const now = local(2026, 6, 15, 12, 0);
  it("names today and yesterday", () => {
    expect(when(local(2026, 6, 15, 9, 42), now)).toBe("Today 09:42");
    expect(when(local(2026, 6, 14, 21, 30), now)).toBe("Yesterday 21:30");
  });
  it("names the weekday within a week and the date after that", () => {
    expect(when(local(2026, 6, 12, 3, 0), now)).toBe("Fri 03:00");
    expect(when(local(2026, 5, 2, 18, 5), now)).toBe("2 May 18:05");
  });
  it("treats a time in the future as today", () => {
    expect(when(local(2026, 6, 16, 1, 0), now)).toBe("Today 01:00");
  });
});

describe("small helpers", () => {
  it("formats a day, counts and lists", () => {
    expect(onDay(local(2026, 6, 15, 1, 0))).toBe("15 Jun 2026");
    expect(count(1, "photo")).toBe("1 photo");
    expect(count(2, "photo")).toBe("2 photos");
    expect(count(2, "person", "people")).toBe("2 people");
    expect(names([], "None")).toBe("None");
    expect(names(["Jo", "Sam"], "None")).toBe("Jo, Sam");
  });
  it("reads the clock in seconds", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(5_000));
    expect(nowSeconds()).toBe(5);
  });
});
