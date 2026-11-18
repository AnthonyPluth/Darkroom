import { afterEach, describe, expect, it, vi } from "vitest";
import { reply, serve } from "../test/server";
import { idLines, ownDevice, pairingMode, sentence } from "./pairing";

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

describe("pairingMode and ownDevice", () => {
  it("are on with Darkroom's own ID", async () => {
    answering({ device_id: "OWN" });
    expect(await pairingMode()).toEqual({ kind: "on" });
    expect(await ownDevice()).toEqual({ kind: "ready", id: "OWN" });
  });

  it("are on but not running when Darkroom's Syncthing doesn't answer", async () => {
    answering(reply(503, { error: "unreachable" }));
    expect(await pairingMode()).toEqual({ kind: "on" });
    expect(await ownDevice()).toEqual({ kind: "not_running" });
  });
});
