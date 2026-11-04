import { describe, expect, it } from "vitest";
import type { EventView } from "./api-types";
import { describeEvent } from "./events";

const event = (type: string, subject = "jo", detail: string | null = null): EventView => ({ id: 1, time: 1, type, subject, detail });

describe("describeEvent", () => {
  it("writes a sentence for each known kind", () => {
    expect(describeEvent(event("person_created"))).toBe("Added jo.");
    expect(describeEvent(event("person_updated"))).toBe("Changed the details of jo.");
    expect(describeEvent(event("person_deleted"))).toBe("Removed jo.");
    expect(describeEvent(event("immich_key_set"))).toBe("Saved an Immich key for jo.");
    expect(describeEvent(event("immich_key_removed"))).toBe("Removed the Immich key of jo.");
    expect(describeEvent(event("account_created", "3"))).toBe("Added a Google account.");
    expect(describeEvent(event("account_updated", "3"))).toBe("Changed a Google account.");
    expect(describeEvent(event("account_deleted", "3"))).toBe("Removed a Google account.");
    expect(describeEvent(event("pixel_created", "3"))).toBe("Added a Pixel.");
    expect(describeEvent(event("pixel_updated", "3"))).toBe("Changed a Pixel.");
    expect(describeEvent(event("pixel_deleted", "3"))).toBe("Removed a Pixel.");
    expect(describeEvent(event("device_revoked", "jo", "Jo's iPhone"))).toBe("Revoked Jo's iPhone for jo.");
    expect(describeEvent(event("device_revoked"))).toBe("Revoked an iPhone for jo.");
    expect(describeEvent(event("device_checkin", "jo", "Jo's iPhone"))).toBe("Jo's iPhone checked in for jo.");
    expect(describeEvent(event("device_checkin"))).toBe("An iPhone checked in for jo.");
    expect(describeEvent(event("originals_indexed", "jo", "new=4 failed=0"))).toBe("Indexed the originals of jo (new=4 failed=0).");
    expect(describeEvent(event("originals_indexed"))).toBe("Indexed the originals of jo.");
    expect(describeEvent(event("index_failed", "jo", "ValueError"))).toBe("Could not index a file of jo: ValueError.");
    expect(describeEvent(event("index_failed"))).toBe("Could not index a file of jo.");
    expect(describeEvent(event("alert_sent", "a@b.c", "sign_in_needed"))).toBe("Sent an alert about a@b.c (sign_in_needed).");
    expect(describeEvent(event("alert_sent", "a@b.c"))).toBe("Sent an alert about a@b.c.");
    expect(describeEvent(event("alert_disabled", "a@b.c"))).toBe("An alert about a@b.c was recorded but not sent.");
    expect(describeEvent(event("alert_failed", "a@b.c"))).toBe("Could not send an alert about a@b.c.");
    expect(describeEvent(event("browser_job_queued", "7", "noop"))).toBe("Queued browser job 7 (noop).");
    expect(describeEvent(event("browser_job_queued", "7"))).toBe("Queued browser job 7.");
    expect(describeEvent(event("browser_job_claimed", "7"))).toBe("The browser worker took job 7.");
    expect(describeEvent(event("browser_job_waiting", "7", "sign_in"))).toBe("Browser job 7 is waiting for a person (sign_in).");
    expect(describeEvent(event("browser_job_waiting", "7"))).toBe("Browser job 7 is waiting for a person.");
    expect(describeEvent(event("browser_job_done", "7"))).toBe("Browser job 7 finished.");
    expect(describeEvent(event("browser_job_failed", "7"))).toBe("Browser job 7 failed.");
  });

  it("falls back to the kind, the subject and the detail", () => {
    expect(describeEvent(event("dedupe_reversed", "jo", "tier 2"))).toBe("Dedupe reversed: jo (tier 2).");
    expect(describeEvent(event("unpacked", "jo"))).toBe("Unpacked: jo.");
  });
});
