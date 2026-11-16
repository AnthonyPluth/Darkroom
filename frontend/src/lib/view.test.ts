import { describe, expect, it } from "vitest";
import { seededStatus } from "../test/server";
import { onDay } from "./format";
import { downloadingExport, expiredExport, failedExport, familyAccount, importedExport, joAccount, preparingExport, queuedConnect, waitingConnect } from "../test/server";
import { accountExports, alertChip, checkNowSentence, currentExport, earlierExports, exportChip, failedCaptures, jobSentence, lastExportSentence, maybeRenewed, renewNow, scheduleLive, partChip, partsLabel, reviewCount, needsAttention, needsSentence, pixelSyncthing, sessionChip, syncthingCard } from "./view";

describe("sessionChip", () => {
  it("names each session state with a tone", () => {
    expect(sessionChip("ok")).toEqual({ tone: "ok", label: "Session ok" });
    expect(sessionChip("needs_sign_in")).toEqual({ tone: "bad", label: "Sign-in needed" });
    expect(sessionChip("unknown")).toEqual({ tone: "neutral", label: "Session not checked yet" });
  });
});

describe("alertChip", () => {
  it("labels the alert kinds", () => {
    expect(alertChip("sign_in_needed")).toEqual({ tone: "bad", label: "Sign-in needed" });
    expect(alertChip("failure")).toEqual({ tone: "bad", label: "Failed" });
    expect(alertChip("originals_unmatched").label).toBe("Check backup");
    expect(alertChip("schedule_ending").label).toBe("Schedule ending");
    expect(alertChip("schedule_renewal_failed")).toEqual({ tone: "bad", label: "Not renewed" });
    expect(alertChip("schedule_renewal_unconfirmed")).toEqual({ tone: "warn", label: "Renewal unconfirmed" });
    expect(alertChip("export_expiring").label).toBe("Export expiring");
    expect(alertChip("syncthing_down")).toEqual({ tone: "bad", label: "Syncthing down" });
    expect(alertChip("syncthing_settings_failed")).toEqual({ tone: "bad", label: "Settings not applied" });
    expect(alertChip("syncthing_version")).toEqual({ tone: "bad", label: "Untested Syncthing" });
    expect(alertChip("share_check_failed")).toEqual({ tone: "bad", label: "Share check failed" });
    expect(alertChip("pixel_held")).toEqual({ tone: "warn", label: "Folder held" });
    expect(alertChip("anything_else")).toEqual({ tone: "warn", label: "Needs a look" });
  });
});

describe("needsAttention and needsSentence", () => {
  it("flags a person with an open alert or an account that needs a sign-in", () => {
    const [jo, sam] = seededStatus.people;
    expect(needsAttention(jo)).toBe(false);
    expect(needsAttention(sam)).toBe(true);
    expect(needsAttention({ ...jo, alerts: [seededStatus.alerts[0]] })).toBe(true);
  });
  it("counts what needs the reader", () => {
    expect(needsSentence(0)).toBe("Nothing needs you right now.");
    expect(needsSentence(1)).toBe("One thing needs you.");
    expect(needsSentence(3)).toBe("3 things need you.");
  });
});

describe("exportChip", () => {
  it("names each state an export can be in", () => {
    expect(exportChip(preparingExport)).toEqual({ tone: "neutral", label: "Google is preparing it" });
    expect(exportChip({ ...preparingExport, ready_at: 5 })).toEqual({ tone: "neutral", label: "Waiting to download" });
    expect(exportChip(downloadingExport)).toEqual({ tone: "neutral", label: "Downloading" });
    expect(exportChip({ ...downloadingExport, status: "downloaded" }).label).toBe("Downloaded");
    expect(exportChip({ ...downloadingExport, status: "unpacking" }).label).toBe("Importing");
    expect(exportChip({ ...downloadingExport, status: "importing" }).label).toBe("Importing");
    expect(exportChip(importedExport)).toEqual({ tone: "ok", label: "Imported" });
    expect(exportChip(failedExport)).toEqual({ tone: "bad", label: "Failed" });
    expect(exportChip(expiredExport)).toEqual({ tone: "warn", label: "Expired" });
    expect(exportChip({ ...expiredExport, status: "mystery" })).toEqual({ tone: "warn", label: "Needs a look" });
  });
});

describe("partChip", () => {
  it("names each state a part can be in", () => {
    const part = downloadingExport.parts[0];
    expect(partChip({ ...part, status: "pending" }, "announced")).toEqual({ tone: "neutral", label: "Waiting" });
    expect(partChip({ ...part, status: "downloading" }, "downloading")).toEqual({ tone: "neutral", label: "Downloading" });
    expect(partChip({ ...part, status: "done" }, "downloading")).toEqual({ tone: "ok", label: "Downloaded" });
    expect(partChip({ ...part, status: "done" }, "imported")).toEqual({ tone: "ok", label: "Imported" });
    expect(partChip({ ...part, status: "failed" }, "failed")).toEqual({ tone: "bad", label: "Failed" });
    expect(partChip({ ...part, status: "corrupt" }, "failed")).toEqual({ tone: "bad", label: "Failed" });
  });
});

describe("the exports of an account", () => {
  const all = [preparingExport, downloadingExport, importedExport, { ...importedExport, id: 99, account_id: 11 }];
  it("keeps one account's exports, takes the newest unfinished one as current and the rest as earlier", () => {
    const mine = accountExports(all, 10);
    expect(mine.map((item) => item.id)).toEqual([41, 40, 38]);
    expect(currentExport(mine)?.id).toBe(41);
    expect(earlierExports(mine, currentExport(mine)).map((item) => item.id)).toEqual([40, 38]);
    expect(currentExport([importedExport, failedExport, expiredExport])).toBeUndefined();
    expect(earlierExports([importedExport], undefined)).toEqual([importedExport]);
  });
  it("counts parts and import reviews", () => {
    expect(partsLabel(null)).toBe("Parts not listed yet");
    expect(partsLabel(1)).toBe("1 part");
    expect(partsLabel(5)).toBe("5 parts");
    const run = { id: 1, export_id: 38, account_id: 10, export_kind: "incremental", export_created_at: 1, part_count: 2, parts: [], started: 1, finished: 2, stage: null, status: "done", added: 0, skipped_t1: 0, skipped_t2: 0, skipped_t3: 0, review: 3, missing_gps: 0, failed: 0, unmatched_by_person: {}, error: null };
    expect(reviewCount([run], 38)).toBe(3);
    expect(reviewCount([run], 39)).toBe(0);
  });
});

describe("lastExportSentence", () => {
  it("sums up the newest export", () => {
    expect(lastExportSentence(undefined)).toBe("Nothing exported yet");
    expect(lastExportSentence(importedExport)).toBe("2 parts, all imported");
    expect(lastExportSentence(expiredExport)).toBe("Expired before it was downloaded");
    expect(lastExportSentence(failedExport)).toBe("2 parts, failed");
    expect(lastExportSentence(preparingExport)).toBe("Google is still preparing it");
    expect(lastExportSentence(downloadingExport)).toBe("3 parts, downloading");
  });
});

describe("checkNowSentence", () => {
  it("says what the check did", () => {
    expect(checkNowSentence(1, 0)).toBe("Checking one Google account for new exports.");
    expect(checkNowSentence(3, 1)).toBe("Checking 3 Google accounts for new exports.");
    expect(checkNowSentence(0, 2)).toBe("A check is already waiting or running.");
    expect(checkNowSentence(0, 0)).toBe("No Google account is connected, so there is nothing to check.");
  });
});

describe("failedCaptures", () => {
  it("includes a failed export check with a capture", () => {
    const job = { ...queuedConnect, id: 9, kind: "status", status: "failed", has_capture: true, capture_files: ["page.html"] };
    expect(failedCaptures([job], 10).map((each) => each.id)).toEqual([9]);
  });

  it("includes a failed renewal with a capture", () => {
    const job = { ...queuedConnect, id: 11, kind: "renew", status: "failed", has_capture: true, capture_files: ["screenshot.png"] };
    expect(failedCaptures([job], 10).map((each) => each.id)).toEqual([11]);
  });
});

describe("maybeRenewed", () => {
  const renew = { ...queuedConnect, id: 15, kind: "renew", status: "failed", message: "ScheduleNotConfirmed: unsure" };
  it("finds the renewal the server says needs a check", () => {
    expect(maybeRenewed([{ ...renew, needs_check: true }], 10)?.id).toBe(15);
  });

  it("ignores a renewal the server does not flag, whatever its message, and other accounts", () => {
    expect(maybeRenewed([renew], 10)).toBeUndefined();
    expect(maybeRenewed([{ ...renew, message: "the worker stopped reporting" }], 10)).toBeUndefined();
    expect(maybeRenewed([{ ...renew, needs_check: true }], 11)).toBeUndefined();
    expect(maybeRenewed([{ ...renew, needs_check: true, kind: "schedule" }], 10)).toBeUndefined();
  });
});

describe("scheduleLive", () => {
  it("is live from its creation until it ends, and not without one", () => {
    expect(scheduleLive(familyAccount, 4_999_999)).toBe(true);
    expect(scheduleLive(familyAccount, 5_000_000)).toBe(false);
    expect(scheduleLive(joAccount, 1)).toBe(false);
    expect(scheduleLive({ ...joAccount, schedule_created: 10 }, 1_000_000)).toBe(true);
  });
});

describe("jobSentence after a check", () => {
  const renew = { ...queuedConnect, kind: "renew", status: "failed", message: "ScheduleNotConfirmed: unsure" };
  it("says what the owner found instead of the failure", () => {
    expect(jobSentence({ ...renew, checked: "found" }, "Renew schedule")).toBe("Renew schedule: not confirmed by Takeout, then you found it there.");
    expect(jobSentence({ ...renew, checked: "missing" }, "Renew schedule")).toBe("Renew schedule: not confirmed by Takeout, and you found none there.");
    expect(jobSentence(renew, "Renew schedule")).toBe("Renew schedule: failed, unsure.");
  });

  it("says an attempt waiting for an answer was not confirmed, never that it failed", () => {
    for (const kind of ["renew", "schedule"]) {
      const sentence = jobSentence({ ...renew, kind, needs_check: true, message: "ScheduleNotConfirmed: Takeout did not confirm a recurring export; check Takeout before trying again" }, "Doing");
      expect(sentence).toBe("Doing: not confirmed by Takeout; say below what you found.");
      expect(sentence).not.toMatch(/failed|trying again/);
    }
  });
});

describe("jobSentence", () => {
  it("tells apart the reasons a job waits for the person", () => {
    const waiting = (message: string) => jobSentence({ ...waitingConnect, message }, "Download");
    expect(waiting("sign_in")).toBe("Download: waiting for you to sign in.");
    expect(waiting("verify")).toBe("Download: waiting for you to confirm your identity in the remote browser.");
    expect(waiting("wrong_account")).toBe("Download: waiting for you to switch to the right Google account.");
    expect(waiting("")).toBe("Download: waiting for you to sign in.");
  });
});

describe("renewNow", () => {
  const NOW = 1_000_000;
  const inWindow = { ...joAccount, schedule_created: 100, schedule_end: NOW + 10 * 86400, renew_from: NOW - 11 * 86400 };
  const job = (kind: string, status: string, extra = {}) => ({ ...queuedConnect, account_id: 10, kind, status, created: NOW - 3600, ...extra });

  it("is not drawn without a schedule or an end date", () => {
    expect(renewNow(joAccount, [], NOW)).toBeNull();
    expect(renewNow({ ...inWindow, schedule_created: null }, [], NOW)).toBeNull();
    expect(renewNow({ ...inWindow, schedule_end: null, renew_from: null }, [], NOW)).toBeNull();
  });

  it("waits for the window and names the day it opens", () => {
    const early = { ...inWindow, renew_from: NOW + 86400 };
    expect(renewNow(early, [], NOW)).toEqual({ enabled: false, sentence: `Darkroom asks for the renewal itself from ${onDay(NOW + 86400)}, three weeks before the schedule ends. Renew now is available from then.` });
    expect(renewNow(inWindow, [], NOW)?.enabled).toBe(true);
    expect(renewNow({ ...inWindow, renew_from: NOW }, [], NOW)?.enabled).toBe(true);
  });

  it("is enabled in the window with nothing wrong", () => {
    expect(renewNow(inWindow, [], NOW)).toEqual({ enabled: true, sentence: "Renew now creates next year's schedule in Takeout without waiting for the daily check. Darkroom does not retry a renewal on its own." });
  });

  it("is disabled while an account needs to sign in, an attempt is unanswered or a job is open", () => {
    expect(renewNow({ ...inWindow, session_state: "needs_sign_in" }, [], NOW)).toEqual({ enabled: false, sentence: "This account needs to sign in to Google again before its schedule can be renewed. Press Connect first." });
    const unanswered = job("renew", "failed", { needs_check: true });
    expect(renewNow(inWindow, [unanswered], NOW)).toEqual({ enabled: false, sentence: "Answer the Takeout question above first." });
    expect(renewNow(inWindow, [job("schedule", "failed", { needs_check: true })], NOW)?.enabled).toBe(false);
    for (const kind of ["schedule", "renew"]) {
      for (const status of ["queued", "running", "waiting_for_user"]) {
        expect(renewNow(inWindow, [job(kind, status)], NOW)).toEqual({ enabled: false, sentence: "A schedule or renew job is already open for this account." });
      }
    }
    expect(renewNow(inWindow, [job("connect", "queued"), job("status", "running"), job("download", "running")], NOW)?.enabled).toBe(true);
  });

  it("follows the route's order when several reasons apply", () => {
    const early = { ...inWindow, renew_from: NOW + 86400, session_state: "needs_sign_in" };
    expect(renewNow(early, [job("renew", "queued")], NOW)?.sentence).toMatch(/^Darkroom asks for the renewal itself/);
    const signIn = { ...inWindow, session_state: "needs_sign_in" };
    expect(renewNow(signIn, [job("renew", "queued")], NOW)?.sentence).toMatch(/^This account needs to sign in/);
    expect(renewNow(inWindow, [job("renew", "queued"), job("schedule", "failed", { needs_check: true })], NOW)?.sentence).toBe("Answer the Takeout question above first.");
  });

  it("says the last renewal failed only for a failure inside this window, and that wins over the ended sentence", () => {
    const failed = job("renew", "failed", { message: "UnexpectedPage: the page changed" });
    expect(renewNow(inWindow, [failed], NOW)).toEqual({ enabled: true, sentence: "The last renewal failed. Darkroom does not retry a renewal on its own: press Renew now to try again." });
    expect(renewNow(inWindow, [{ ...failed, created: inWindow.renew_from - 1 }], NOW)?.sentence).toMatch(/^Renew now creates next year's schedule/);
    expect(renewNow(inWindow, [job("renew", "done")], NOW)?.sentence).toMatch(/^Renew now creates next year's schedule/);
    const ended = { ...inWindow, schedule_end: NOW - 86400 };
    expect(renewNow(ended, [failed], NOW)?.sentence).toMatch(/^The last renewal failed/);
  });

  it("is enabled after the schedule ended and says scheduled exports have stopped", () => {
    const ended = { ...inWindow, schedule_end: NOW - 86400 };
    expect(renewNow(ended, [], NOW)).toEqual({ enabled: true, sentence: `This schedule ended on ${onDay(NOW - 86400)}, so scheduled exports have stopped. Renew now or Create schedule sets up a new yearly schedule in Takeout; both run the same steps.` });
  });
});

describe("pixelSyncthing", () => {
  const now = 100_000;
  const base = { pixel_id: 1, name: "Pixel 8", copies: 0, files: 0, days: 0, completion: null, last_seen: null, others: [] };

  it("gives each per-Pixel state its chip and line", () => {
    expect(pixelSyncthing({ ...base, state: "not_syncing" }, now)).toEqual({ tone: "neutral", label: "Not syncing", lines: ["Nothing syncs until Syncthing is running again."] });
    expect(pixelSyncthing({ ...base, state: "not_paired" }, now)).toEqual({ tone: "neutral", label: "Not paired", lines: ["Pair it to start sending photos."] });
    expect(pixelSyncthing({ ...base, state: "checking" }, now)).toEqual({ tone: "neutral", label: "Checking", lines: ["Darkroom is checking its folder before sharing it."] });
    expect(pixelSyncthing({ ...base, state: "waiting" }, now)).toEqual({ tone: "neutral", label: "Waiting for the Pixel", lines: ["Paired; the Pixel has not connected yet."] });
    expect(pixelSyncthing({ ...base, state: "not_seen", days: 3 }, now).label).toBe("Not seen for 3 days");
    expect(pixelSyncthing({ ...base, state: "sending", completion: 99.9 }, now)).toEqual({ tone: "ok", label: "Sending", lines: ["99% sent"] });
    expect(pixelSyncthing({ ...base, state: "connected", completion: 100 }, now)).toEqual({ tone: "ok", label: "Connected", lines: ["Up to date"] });
    expect(pixelSyncthing({ ...base, state: "not_connected", last_seen: now - 7200 }, now)).toEqual({ tone: "neutral", label: "Not connected", lines: ["Last seen 2 hours ago."] });
  });

  it("gives the three states with no folder and the refused folder a warn chip and their line", () => {
    expect(pixelSyncthing({ ...base, state: "device_id_invalid" }, now)).toEqual({ tone: "warn", label: "Device ID not valid", lines: ["Its saved device ID is not a Syncthing device ID, so nothing is shared with it. Pair it again."] });
    expect(pixelSyncthing({ ...base, state: "folder_removed" }, now)).toEqual({ tone: "warn", label: "Folder removed", lines: ["Darkroom removed its folder because it was changed outside Darkroom. It stays unshared until you share it again."] });
    expect(pixelSyncthing({ ...base, state: "folder_path_refused" }, now)).toEqual({ tone: "warn", label: "Folder refused", lines: ["Its folder leads outside the photo share, so Darkroom does not share it."] });
  });

  it("names the other Pixels with the same device ID, with has for one and have for more", () => {
    const shared = { ...base, state: "device_id_shared" as const };
    expect(pixelSyncthing({ ...shared, others: ["Casey's Pixel"] }, now)).toEqual({ tone: "warn", label: "Same device ID", lines: ["Casey's Pixel has the same device ID, so Darkroom shares neither folder. Pair again whichever one is wrong."] });
    expect(pixelSyncthing({ ...shared, others: ["A", "B"] }, now).lines).toEqual(["A and B have the same device ID, so Darkroom shares neither folder. Pair again whichever one is wrong."]);
    expect(pixelSyncthing({ ...shared, others: ["A", "B", "C"] }, now).lines).toEqual(["A, B and C have the same device ID, so Darkroom shares neither folder. Pair again whichever one is wrong."]);
  });

  it("keeps the Folder held chip and shows both causes, counting one photo and one file in the singular", () => {
    expect(pixelSyncthing({ ...base, state: "held", copies: 1, files: 1 }, now)).toEqual({
      tone: "warn", label: "Folder held",
      lines: ["1 photo could not be written again, so its folder is not shared.", "Its folder has 1 file Darkroom did not put there, so it is not shared."],
    });
    expect(pixelSyncthing({ ...base, state: "held", copies: 0, files: 2 }, now).lines).toEqual(["Its folder has 2 files Darkroom did not put there, so it is not shared."]);
  });
});

describe("syncthingCard", () => {
  it("says how long Syncthing has not answered", () => {
    const down = { state: "down" as const, reason: null, since: 0, pixels: [] };
    expect(syncthingCard(down, 30).line).toMatch(/^Syncthing has not answered for less than a minute\./);
    expect(syncthingCard(down, 45 * 60).line).toMatch(/^Syncthing has not answered for 45 minutes\./);
    expect(syncthingCard(down, 3600).line).toMatch(/^Syncthing has not answered for 1 hour\./);
    expect(syncthingCard(down, 3 * 86_400).line).toMatch(/^Syncthing has not answered for 3 days\./);
    expect(syncthingCard({ ...down, since: null }, 99).line).toMatch(/^Syncthing has not answered for less than a minute\./);
  });
});
