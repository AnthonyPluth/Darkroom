const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export function ago(then: number | null, now: number): string {
  if (then === null) return "never";
  const seconds = Math.max(0, now - then);
  if (seconds < MINUTE) return "just now";
  if (seconds < HOUR) return `${plural(Math.floor(seconds / MINUTE), "minute")} ago`;
  if (seconds < DAY) return `${plural(Math.floor(seconds / HOUR), "hour")} ago`;
  return `${plural(Math.floor(seconds / DAY), "day")} ago`;
}

const clock = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
const weekday = new Intl.DateTimeFormat("en-GB", { weekday: "short" });
const dayMonth = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
const dayMonthYear = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });
const dayMonthYearClock = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });

function startOfDay(seconds: number): number {
  const date = new Date(seconds * 1000);
  date.setHours(0, 0, 0, 0);
  return date.getTime() / 1000;
}

export function when(then: number, now: number): string {
  const time = clock.format(new Date(then * 1000));
  const days = Math.round((startOfDay(now) - startOfDay(then)) / DAY);
  if (days <= 0) return `Today ${time}`;
  if (days === 1) return `Yesterday ${time}`;
  if (days < 7) return `${weekday.format(new Date(then * 1000))} ${time}`;
  return `${dayMonth.format(new Date(then * 1000))} ${time}`;
}

export function onDay(then: number): string {
  return dayMonthYear.format(new Date(then * 1000));
}

export function onDayAt(then: number): string {
  return dayMonthYearClock.format(new Date(then * 1000));
}

export function untilDay(then: number, now: number): string {
  if (then <= now) return "Ended";
  const days = Math.ceil((then - now) / DAY);
  if (days < 14) return days <= 1 ? "Within a day" : `In ${plural(days, "day")}`;
  return `In ${plural(Math.round(days / 7), "week")}`;
}

export function count(n: number, one: string, many?: string): string {
  return plural(n, one, many);
}

export function names(list: readonly string[], none: string): string {
  return list.length ? list.join(", ") : none;
}

export function nowSeconds(): number {
  return Date.now() / 1000;
}

export function checkedIn(then: number | null, now: number): string {
  return then === null ? "never checked in" : `checked in ${ago(then, now)}`;
}
