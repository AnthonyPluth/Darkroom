// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PixelView, WaitingDevice } from "$lib/api-types";
import { reply, serve, shared } from "../test/server";
import PixelPairing from "./PixelPairing.svelte";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const OWN = "DRKROOM-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG";
const PHONE: WaitingDevice = { device_id: "PIXELNA-MADEUP2-EXAMPLE-ONLYAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE", name: "Pixel 8" };
const OTHER: WaitingDevice = { device_id: "NOTTHIS-MADEUP3-EXAMPLE-ONLYZZZ-YYYYYYY-XXXXXXX-WWWWWWW-VVVVVVV", name: "Pixel" };
const paired: PixelView = { ...shared, syncthing_device_id: PHONE.device_id };

function mount(routes: Parameters<typeof serve>[0]) {
  const server = serve({ "GET /api/pairing/device": { device_id: OWN }, ...routes });
  vi.stubGlobal("fetch", server.mock);
  const onpaired = vi.fn();
  const onlater = vi.fn();
  const view = render(PixelPairing, { pixel: shared, onpaired, onlater });
  return { server, onpaired, onlater, view };
}

const waiting = () => screen.getByRole("status");

describe("waiting for the Pixel", () => {
  it("shows Darkroom's code and ID, then says it is waiting while nobody has connected", async () => {
    const { server, onlater } = mount({ "GET /api/pairing/waiting": { devices: [] } });
    expect(await screen.findByRole("img", { name: "QR code of Darkroom's device ID" })).toHaveAttribute("data-modules", "37");
    expect(screen.getByText("DRKROOM-AAAAAAA-BBBBBBB-CCCCCCC")).toBeInTheDocument();
    expect(screen.getByText("DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "1. Scan Darkroom's code" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "2. Approve the Pixel" })).toBeInTheDocument();
    await waitFor(() => expect(waiting()).toHaveTextContent("Waiting for Pixel 8 to connect. Darkroom checks again every few seconds."));
    expect(screen.queryByRole("button", { name: /^This is/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Check now" }));
    await waitFor(() => expect(server.sent("GET /api/pairing/waiting")).toHaveLength(2));
    await userEvent.click(screen.getByRole("button", { name: "Pair later" }));
    expect(onlater).toHaveBeenCalledOnce();
  });

  it("lists one waiting device with its name, its ID on two lines and a button for this Pixel", async () => {
    mount({ "GET /api/pairing/waiting": { devices: [PHONE] } });
    const list = await screen.findByRole("status");
    await waitFor(() => expect(within(list).getByRole("button", { name: "This is Pixel 8" })).toBeInTheDocument());
    expect(within(list).getByText("PIXELNA-MADEUP2-EXAMPLE-ONLYAAA")).toBeInTheDocument();
    expect(within(list).getByText("BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE")).toBeInTheDocument();
    expect(within(list).getByText("Check that this matches the device ID the Syncthing app shows for Pixel 8 itself.")).toBeInTheDocument();
    expect(within(list).queryByText(/More than one device/)).not.toBeInTheDocument();
  });

  it("warns when more than one device is waiting, in Syncthing's order", async () => {
    mount({ "GET /api/pairing/waiting": { devices: [PHONE, OTHER] } });
    expect(await screen.findByText("More than one device is waiting. Approve only the one whose ID matches the Pixel.")).toBeInTheDocument();
    expect(within(waiting()).getAllByRole("button", { name: "This is Pixel 8" })).toHaveLength(2);
  });

  it("says why the list can't be read, keeps asking, and drops the line at the next good read", async () => {
    let attempt = 0;
    mount({ "GET /api/pairing/waiting": () => (attempt++ === 0 ? reply(503, { error: "Darkroom's Syncthing could not be reached; try again in a moment" }) : { devices: [] }) });
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't check for waiting devices: Darkroom's Syncthing could not be reached; try again in a moment.");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Check now" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(waiting()).toHaveTextContent("Waiting for Pixel 8 to connect.");
  });
});

describe("polling", () => {
  it("reads the list every 5 seconds while the tab is visible, pauses while approving and stops once closed", async () => {
    vi.useFakeTimers();
    const { server, view } = mount({ "GET /api/pairing/waiting": { devices: [PHONE] } });
    const reads = () => server.sent("GET /api/pairing/waiting").length;
    await vi.waitFor(() => expect(reads()).toBe(1));
    await vi.advanceTimersByTimeAsync(5000);
    expect(reads()).toBe(2);
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads()).toBe(2);
    visibility.mockReturnValue("visible");
    await vi.advanceTimersByTimeAsync(5000);
    expect(reads()).toBe(3);
    await fireEvent.click(await vi.waitFor(() => screen.getByRole("button", { name: "This is Pixel 8" })));
    await vi.advanceTimersByTimeAsync(15_000);
    expect(reads()).toBe(3);
    await fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await vi.waitFor(() => expect(reads()).toBe(4));
    await vi.advanceTimersByTimeAsync(5000);
    expect(reads()).toBe(5);
    view.unmount();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(reads()).toBe(5);
  });
});

describe("approving", () => {
  it("asks again in its own box before anything is paired, and Back returns to the list", async () => {
    const { server } = mount({ "GET /api/pairing/waiting": { devices: [PHONE] } });
    await userEvent.click(await screen.findByRole("button", { name: "This is Pixel 8" }));
    expect(screen.getByRole("heading", { name: "Approve this device for Pixel 8?" })).toBeInTheDocument();
    expect(screen.getByText("Darkroom will share Pixel 8's folder with this device and with nothing else. Approve it only if the ID matches the one on the Pixel.")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(server.sent("POST /api/pixels/21/pair")).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("button", { name: "This is Pixel 8" })).toBeInTheDocument();
    expect(server.sent("POST /api/pixels/21/pair")).toHaveLength(0);
  });

  it("sends the device with the confirmation, holds both buttons while it is in flight, and reports the Pixel paired", async () => {
    let answer: (value: Response) => void = () => {};
    const { server, onpaired } = mount({ "GET /api/pairing/waiting": { devices: [PHONE] } });
    const original = server.mock.getMockImplementation();
    server.mock.mockImplementation((url: string, init: RequestInit = {}) => {
      if (url === "/api/pixels/21/pair") {
        server.calls.push({ key: `POST ${url}`, body: JSON.parse(String(init.body)) });
        return new Promise<Response>((resolve) => (answer = resolve));
      }
      return original!(url, init);
    });
    await userEvent.click(await screen.findByRole("button", { name: "This is Pixel 8" }));
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(server.sent("POST /api/pixels/21/pair")[0].body).toEqual({ device_id: PHONE.device_id, confirmed: true });
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Back" })).toBeDisabled();
    answer(new Response(JSON.stringify({ pixel: paired }), { status: 200 }));
    await waitFor(() => expect(onpaired).toHaveBeenCalledWith(paired));
  });

  it("shows Darkroom's refusal as a sentence, goes back to the list read again at once, and drops the line at the next press", async () => {
    const { server, onpaired } = mount({
      "GET /api/pairing/waiting": { devices: [PHONE] },
      "POST /api/pixels/21/pair": reply(409, { error: "that device is no longer waiting; ask the Pixel to connect again" }),
    });
    await userEvent.click(await screen.findByRole("button", { name: "This is Pixel 8" }));
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't pair Pixel 8: that device is no longer waiting; ask the Pixel to connect again.");
    expect(screen.queryByRole("heading", { name: "Approve this device for Pixel 8?" })).not.toBeInTheDocument();
    await waitFor(() => expect(server.sent("GET /api/pairing/waiting")).toHaveLength(2));
    expect(onpaired).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Check now" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("typing the ID", () => {
  it("opens the field under the list, saves through the Pixel's own validation and reports the Pixel paired", async () => {
    const { server, onpaired } = mount({ "GET /api/pairing/waiting": { devices: [] }, "PATCH /api/pixels/21": { pixel: paired } });
    await userEvent.click(await screen.findByRole("button", { name: "Type the device ID instead" }));
    expect(screen.getByRole("img", { name: "QR code of Darkroom's device ID" })).toBeInTheDocument();
    const field = screen.getByLabelText("Syncthing device ID");
    expect(field).toHaveAccessibleDescription("Eight groups of seven letters and digits, as the Syncthing app on the Pixel shows it.");
    await userEvent.type(field, ` ${PHONE.device_id} `);
    await userEvent.click(screen.getByRole("button", { name: "Save device ID" }));
    expect(server.sent("PATCH /api/pixels/21")[0].body).toEqual({ syncthing_device_id: PHONE.device_id });
    await waitFor(() => expect(onpaired).toHaveBeenCalledWith(paired));
  });

  it("shows the validator's text under the field when Darkroom refuses it", async () => {
    const { onpaired } = mount({ "GET /api/pairing/waiting": { devices: [] }, "PATCH /api/pixels/21": reply(400, { error: "not a Syncthing device ID" }) });
    await userEvent.click(await screen.findByRole("button", { name: "Type the device ID instead" }));
    await userEvent.type(screen.getByLabelText("Syncthing device ID"), "PIXELNA-MADEUP2");
    await userEvent.click(screen.getByRole("button", { name: "Save device ID" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't pair Pixel 8: not a Syncthing device ID.");
    expect(onpaired).not.toHaveBeenCalled();
  });

  it("is the only way when Darkroom's Syncthing is not running, with no code and no list", async () => {
    const { server } = mount({ "GET /api/pairing/device": reply(503, { error: "Darkroom's Syncthing could not be reached" }) });
    expect(await screen.findByText("Darkroom's Syncthing is not running, so the Pixel can't be paired by scanning now. Type the Pixel's device ID instead; it connects once Syncthing is running again.")).toBeInTheDocument();
    expect(screen.getByLabelText("Syncthing device ID")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Type the device ID instead" })).not.toBeInTheDocument();
    expect(server.sent("GET /api/pairing/waiting")).toHaveLength(0);
  });
});
