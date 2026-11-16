import type { AccountView, PersonView, PixelView } from "$lib/api-types";
import { apiCall } from "$lib/contract";
import { nowSeconds } from "$lib/format";

export type Family = { people: PersonView[]; accounts: AccountView[]; pixels: PixelView[]; now: number };

export type Editing =
  | { kind: "person"; item: PersonView | null }
  | { kind: "account"; item: AccountView | null }
  | { kind: "pixel"; item: PixelView | null };

export async function loadFamily(): Promise<Family> {
  const [people, accounts, pixels] = await Promise.all([
    apiCall<"GET /api/people">("/api/people", { failed: "Couldn't load the people" }),
    apiCall<"GET /api/accounts">("/api/accounts", { failed: "Couldn't load the Google accounts" }),
    apiCall<"GET /api/pixels">("/api/pixels", { failed: "Couldn't load the Pixels" }),
  ]);
  return { people: people.people, accounts: accounts.accounts, pixels: pixels.pixels, now: nowSeconds() };
}

export const personName = (people: PersonView[], id: number): string => people.find((p) => p.id === id)?.name ?? "Unknown";

export function pixelLabel(pixel: PixelView, person: PersonView, people: PersonView[]): string {
  const others = pixel.people_ids.filter((id) => id !== person.id).map((id) => personName(people, id));
  return others.length ? `${pixel.name} (also ${others.join(", ")})` : pixel.name;
}

export function formTitle(next: Editing): string {
  const noun = next.kind === "person" ? "person" : next.kind === "account" ? "Google account" : "Pixel";
  return next.item ? `Edit ${noun}` : `Add ${noun}`;
}
