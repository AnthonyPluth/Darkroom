import { afterEach, describe, expect, it, vi } from "vitest";
import { reply, serve } from "../test/server";
import { OFFLINE } from "./api";
import { idLines, ownDevice, PAIRING_FAILED, readPairing, sentence } from "./pairing";

afterEach(() => vi.unstubAllGlobals());

const answering = (answer: unknown) => vi.stubGlobal("fetch", serve({ "GET /api/pairing/device": answer }).mock);

describe("idLines", () => {
  it("splits a device ID after its fourth group and leaves a short one whole", () => {
    expect(idLines("AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH")).toEqual(["AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD", "EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"]);
    expect(idLines("DEMO-CASEY-PIXEL")).toEqual(["DEMO-CASEY-PIXEL"]);
  });
});

describe("sentence", () => {
  it("ends a message with a full stop once", () => {
    expect(sentence("not a Syncthing device ID")).toBe("not a Syncthing device ID.");
    expect(sentence("Can't reach Darkroom. Check your connection and try again.")).toBe("Can't reach Darkroom. Check your connection and try again.");
  });
});

describe("readPairing and ownDevice", () => {
  it("are on with Darkroom's own ID", async () => {
    answering({ device_id: "OWN" });
    await expect(readPairing()).resolves.toBeUndefined();
    expect(await ownDevice()).toEqual({ kind: "ready", id: "OWN" });
  });

  it("are on but not running when Darkroom's Syncthing doesn't answer", async () => {
    answering(reply(503, { error: "unreachable" }));
    await expect(readPairing()).resolves.toBeUndefined();
    expect(await ownDevice()).toEqual({ kind: "not_running" });
  });

  it("can't be read when the pairing route answers anything but success or 503", async () => {
    answering(reply(500, {}));
    await expect(readPairing()).rejects.toMatchObject({ message: `${PAIRING_FAILED} (500)`, status: 500 });
    answering(reply(409, {}));
    await expect(readPairing()).rejects.toMatchObject({ message: `${PAIRING_FAILED} (409)`, status: 409 });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(readPairing()).rejects.toMatchObject({ message: OFFLINE, status: 0 });
  });
});
