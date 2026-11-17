import type { SetupSetting, SetupState } from "$lib/api-types";

export const STEPS = [
  { key: "address", name: "Address" },
  { key: "sign-in", name: "Sign-in" },
  { key: "immich", name: "Immich" },
  { key: "people", name: "People" },
  { key: "accounts", name: "Google accounts" },
  { key: "pixels", name: "Pixels" },
  { key: "alerts", name: "Alert emails" },
  { key: "done", name: "Done" },
] as const;

export type Step = (typeof STEPS)[number]["key"];
export type Mark = "done" | "skipped";
export type Marks = Partial<Record<Step, Mark>>;
export type Counts = { people: number; accounts: number; pixels: number };

export const WIDE_STEPS: readonly Step[] = ["people", "accounts", "pixels"];
export const ALERT_VARIABLES = ["RESEND_API_KEY", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO"] as const;
export const ENVIRONMENT_ROW = "Set in Darkroom's environment.";
export const ENVIRONMENT_HINT = "The environment always wins over setup. To change it, change the container's settings.";

const STORE = { step: "darkroom.setup.step", marks: "darkroom.setup.marks", testing: "darkroom.setup.testing" };
const AFTER_TEST: readonly Step[] = ["immich", "people", "accounts", "pixels", "alerts", "done"];

export const stepIndex = (step: Step): number => STEPS.findIndex((s) => s.key === step);
export const stepName = (step: Step): string => STEPS[stepIndex(step)].name;
export const stepLine = (step: Step): string => `Step ${stepIndex(step) + 1} of ${STEPS.length}: ${stepName(step)}`;
export const isStep = (value: unknown): value is Step => STEPS.some((s) => s.key === value);

export function settingOf(state: SetupState, variable: string): SetupSetting {
  return state.settings.find((s) => s.variable === variable) ?? { variable, source: "default", set: false, value: null };
}

export const fromEnvironment = (state: SetupState, variable: string): boolean => settingOf(state, variable).source === "environment";

export const storedValue = (state: SetupState, variable: string): string | null => {
  const found = settingOf(state, variable);
  return found.source === "stored" ? found.value : null;
};

export function failed(what: string, message: string): string {
  const text = `Couldn't ${what}: ${message.trim()}`;
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

export function splitList(text: string): string[] {
  return text.split(",").map((part) => part.trim()).filter(Boolean);
}

export const joinList = (list: readonly string[]): string => list.join(", ");

export function alertsOn(state: SetupState): boolean {
  return ALERT_VARIABLES.every((v) => settingOf(state, v).set);
}

export function firstStep(state: SetupState, counts: Counts | null, remembered: Step | null): Step {
  if (state.phase === "before_test") return settingOf(state, "DARKROOM_PUBLIC_URL").set ? "sign-in" : "address";
  if (!settingOf(state, "IMMICH_URL").set) return "immich";
  if (counts && counts.people === 0) return "people";
  if (remembered && AFTER_TEST.includes(remembered)) return remembered;
  if (counts && counts.accounts === 0) return "accounts";
  return "done";
}

export function markOf(step: Step, state: SetupState, counts: Counts | null, marks: Marks): Mark | null {
  const after = state.phase === "after_test";
  switch (step) {
    case "address": return after || settingOf(state, "DARKROOM_PUBLIC_URL").set ? "done" : null;
    case "sign-in": return after ? "done" : null;
    case "immich": return after && settingOf(state, "IMMICH_URL").set ? "done" : null;
    case "people": return counts && counts.people > 0 ? "done" : null;
    case "accounts": return counts && counts.accounts > 0 ? "done" : marks.accounts ?? null;
    case "pixels": return counts && counts.pixels > 0 ? "done" : marks.pixels ?? null;
    case "alerts": return after && alertsOn(state) ? "done" : marks.alerts ?? null;
    default: return null;
  }
}

export function linked(step: Step, state: SetupState, mark: Mark | null): boolean {
  if (!mark) return false;
  if (state.phase === "after_test") return AFTER_TEST.includes(step);
  return step === "address";
}

function read(key: string): string | null {
  try { return sessionStorage.getItem(key); } catch { return null; }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch { return; }
}

export function rememberedStep(): Step | null {
  const found = read(STORE.step);
  return isStep(found) ? found : null;
}

export const rememberStep = (step: Step): void => write(STORE.step, step);

export function rememberedMarks(): Marks {
  try {
    const parsed: unknown = JSON.parse(read(STORE.marks) ?? "{}");
    if (!parsed || typeof parsed !== "object") return {};
    return Object.fromEntries(Object.entries(parsed).filter(([k, v]) => isStep(k) && (v === "done" || v === "skipped"))) as Marks;
  } catch { return {}; }
}

export const rememberMarks = (marks: Marks): void => write(STORE.marks, JSON.stringify(marks));

export const startTesting = (): void => write(STORE.testing, "1");

export function arrivedFromTest(): boolean {
  const found = read(STORE.testing) === "1";
  write(STORE.testing, null);
  return found;
}

export const ROW = "flex flex-col gap-3 md:flex-row md:flex-wrap md:items-center";
export const BUTTON = "btn min-h-12 md:min-h-11";
export const PRIMARY = "btn btn-primary min-h-12 md:min-h-11";

export const browser = { leave: (url: string): void => location.assign(url) };

export const COPY_FAILED = "Couldn't copy the callback URL. It is selected above: copy it with your keyboard or the browser's menu.";

function copyWithSelection(text: string): boolean {
  const before = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const area = document.createElement("textarea");
  area.value = text;
  area.readOnly = true;
  area.className = "fixed top-0 left-0 opacity-0";
  document.body.append(area);
  area.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area.remove();
    before?.focus();
  }
}

export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return copyWithSelection(text);
    }
  }
  return copyWithSelection(text);
}

export function selectText(element: HTMLElement): void {
  const range = document.createRange();
  range.selectNodeContents(element);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
}
