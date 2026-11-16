<script lang="ts">
  import { onMount, untrack } from "svelte";
  import { actGet, errMsg } from "$lib/act";
  import type { PixelView, WaitingDevice } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import { idLines, ownDevice, POLL_MS, sentence, type OwnDevice } from "$lib/pairing";
  import Field from "../components/Field.svelte";
  import QrCode from "../components/QrCode.svelte";
  import FormError from "./FormError.svelte";

  let { pixel, onpaired, onlater }: { pixel: PixelView; onpaired: (pixel: PixelView) => void; onlater: () => void } = $props();

  const target = untrack(() => pixel);
  const name = target.name;

  let own = $state<OwnDevice>({ kind: "loading" });
  let devices = $state.raw<WaitingDevice[] | null>(null);
  let listError = $state("");
  let choosing = $state.raw<WaitingDevice | null>(null);
  let approving = $state(false);
  let refusal = $state("");
  let refusedTyping = $state(false);
  let typing = $state(false);
  let typed = $state("");
  let saving = $state(false);
  let paired = false;
  let asked = 0;

  async function read(): Promise<void> {
    const order = ++asked;
    try {
      const found = await apiCall<"GET /api/pairing/waiting">("/api/pairing/waiting");
      if (order !== asked || paired) return;
      devices = found.devices;
      listError = "";
    } catch (err) {
      if (order !== asked || paired) return;
      devices = null;
      listError = `Couldn't check for waiting devices: ${sentence(errMsg(err))}`;
    }
  }

  onMount(() => {
    void ownDevice().then((found) => {
      own = found;
      if (found.kind === "ready") void read();
      else typing = true;
    });
  });

  $effect(() => {
    if (own.kind !== "ready" || choosing !== null) return;
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") void read();
    }, POLL_MS);
    return () => clearInterval(timer);
  });

  function pressed(event: MouseEvent): void {
    if (event.target instanceof Element && event.target.closest("button")) refusal = "";
  }

  function refuse(message: string, whileTyping: boolean): void {
    refusal = `Couldn't pair ${name}: ${sentence(message)}`;
    refusedTyping = whileTyping;
  }

  function done(saved: PixelView): void {
    paired = true;
    onpaired(saved);
  }

  function back(): void {
    choosing = null;
    void read();
  }

  async function approve(device: WaitingDevice): Promise<void> {
    const saved = await actGet(
      () => apiCall<"POST /api/pixels/{id}/pair">(`/api/pixels/${target.id}/pair`, { method: "POST", body: { device_id: device.device_id, confirmed: true } }),
      { busy: (on) => (approving = on), onError: (message) => refuse(message, false) },
    );
    if (saved) {
      done(saved.pixel);
      return;
    }
    choosing = null;
    void read();
  }

  async function save(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const saved = await actGet(
      () => apiCall<"PATCH /api/pixels/{id}">(`/api/pixels/${target.id}`, { method: "PATCH", body: { syncthing_device_id: typed.trim() } }),
      { busy: (on) => (saving = on), onError: (message) => refuse(message, true) },
    );
    if (saved) done(saved.pixel);
  }
</script>

{#snippet deviceId(id: string)}
  <p class="font-mono text-sm break-all">
    {#each idLines(id) as line, i (i)}<span class="block">{line}</span>{/each}
  </p>
{/snippet}

<div class="flex flex-col gap-4" role="presentation" onclickcapture={pressed}>
  {#if own.kind === "not_running"}
    <p>Darkroom's Syncthing is not running, so the Pixel can't be paired by scanning now. Type the Pixel's device ID instead; it connects once Syncthing is running again.</p>
  {:else if own.kind === "ready"}
    <h3 class="font-semibold">1. Scan Darkroom's code</h3>
    <p>On the Pixel, open the Syncthing app, add a device and scan this code.</p>
    <QrCode text={own.id} label="QR code of Darkroom's device ID" />
    <div class="flex flex-col gap-1">
      <span class="text-ink-muted">Darkroom's device ID</span>
      {@render deviceId(own.id)}
    </div>
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h3 class="font-semibold">2. Approve the Pixel</h3>
      <button type="button" class="btn" onclick={() => void read()}>Check now</button>
    </div>
    {#if choosing}
      {@const device = choosing}
      <div class="flex flex-col gap-2 rounded-md border border-line p-3">
        <h3 class="font-semibold">Approve this device for {name}?</h3>
        <b>{device.name}</b>
        {@render deviceId(device.device_id)}
        <p>Darkroom will share {name}'s folder with this device and with nothing else. Approve it only if the ID matches the one on the Pixel.</p>
        <div class="flex flex-wrap items-center justify-between gap-3">
          <button type="button" class="btn" disabled={approving} onclick={back}>Back</button>
          <button type="button" class="btn btn-primary" disabled={approving} onclick={() => void approve(device)}>Approve</button>
        </div>
      </div>
    {:else if listError}
      <FormError message={listError} />
    {:else}
      <div role="status" class="flex flex-col gap-2">
        {#if devices === null || devices.length === 0}
          <p class="text-ink-muted">Waiting for {name} to connect. Darkroom checks again every few seconds.</p>
        {:else}
          {#if devices.length > 1}
            <p>More than one device is waiting. Approve only the one whose ID matches the Pixel.</p>
          {/if}
          {#each devices as device (device.device_id)}
            <div class="flex flex-col gap-2 rounded-md border border-line p-3">
              <b>{device.name}</b>
              {@render deviceId(device.device_id)}
              <p class="text-ink-muted">Check that this matches the device ID the Syncthing app shows for {name} itself.</p>
              <button type="button" class="btn btn-primary self-start" onclick={() => (choosing = device)}>This is {name}</button>
            </div>
          {/each}
        {/if}
      </div>
    {/if}
    {#if !refusedTyping}<FormError message={refusal} />{/if}
  {/if}

  {#if typing}
    <form class="flex flex-col gap-4" onsubmit={save}>
      <Field label="Syncthing device ID" hint="Eight groups of seven letters and digits, as the Syncthing app on the Pixel shows it.">
        {#snippet children(field)}
          <input id={field.id} aria-describedby={field.describedby} class="input font-mono" name="syncthing_device_id" required maxlength="80" autocomplete="off" spellcheck="false" bind:value={typed} />
        {/snippet}
      </Field>
      {#if refusedTyping}<FormError message={refusal} />{/if}
      <button type="submit" class="btn btn-primary self-start" disabled={saving}>Save device ID</button>
    </form>
  {:else if own.kind === "ready"}
    <button type="button" class="min-h-11 self-start font-semibold underline" onclick={() => (typing = true)}>Type the device ID instead</button>
  {/if}

  <div class="flex flex-wrap items-center justify-between gap-3">
    <button type="button" class="btn" onclick={onlater}>Pair later</button>
  </div>
</div>
