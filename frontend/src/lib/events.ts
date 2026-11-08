import type { EventView } from "./api-types";

const SENTENCES: Record<string, (e: EventView) => string> = {
  person_created: (e) => `Added ${e.subject}.`,
  person_updated: (e) => `Changed the details of ${e.subject}.`,
  person_deleted: (e) => `Removed ${e.subject}.`,
  immich_key_set: (e) => `Saved an Immich key for ${e.subject}.`,
  immich_key_removed: (e) => `Removed the Immich key of ${e.subject}.`,
  account_created: () => "Added a Google account.",
  account_updated: () => "Changed a Google account.",
  account_deleted: () => "Removed a Google account.",
  pixel_created: () => "Added a Pixel.",
  pixel_updated: () => "Changed a Pixel.",
  pixel_deleted: () => "Removed a Pixel.",
  originals_indexed: (e) => `Indexed the originals of ${e.subject}${e.detail ? ` (${e.detail})` : ""}.`,
  index_failed: (e) => `Could not index a file of ${e.subject}${e.detail ? `: ${e.detail}` : ""}.`,
  alert_sent: (e) => `Sent an alert about ${e.subject}${e.detail ? ` (${e.detail})` : ""}.`,
  alert_disabled: (e) => `An alert about ${e.subject} was recorded but not sent.`,
  alert_failed: (e) => `Could not send an alert about ${e.subject}.`,
  browser_job_queued: (e) => `Queued browser job ${e.subject}${e.detail ? ` (${e.detail})` : ""}.`,
  browser_job_claimed: (e) => `The browser worker took job ${e.subject}.`,
  browser_job_waiting: (e) => `Browser job ${e.subject} is waiting for a person${e.detail ? ` (${e.detail})` : ""}.`,
  browser_job_done: (e) => `Browser job ${e.subject} finished.`,
  browser_job_failed: (e) => `Browser job ${e.subject} failed.`,
};

export function describeEvent(e: EventView): string {
  const known = SENTENCES[e.type];
  if (known) return known(e);
  const words = e.type.replace(/_/g, " ");
  return `${words.charAt(0).toUpperCase()}${words.slice(1)}: ${e.subject}${e.detail ? ` (${e.detail})` : ""}.`;
}
