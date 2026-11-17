<script lang="ts">
  import FormError from "../forms/FormError.svelte";
  import { PRIMARY, ROW } from "./wizard";

  let { busy, error, onstart }: { busy: boolean; error: string; onstart: () => void } = $props();
</script>

<h1 tabindex="-1" class="text-xl font-bold tracking-tight">Set up Darkroom</h1>
<p>This sets Darkroom up on this server, from this browser, from the first sign-in to the people, Google accounts and Pixels. You'll need:</p>
<ul class="flex list-disc flex-col gap-1.5 pl-5">
  <li>The address you open Darkroom at (this one, most likely).</li>
  <li>A client for Darkroom in Pocket ID: its issuer address, client ID and client secret.</li>
  <li>Immich's address, and an Immich API key for each person.</li>
  <li>Each person's Google account and its password, to sign in once in Darkroom's remote browser.</li>
  <li>Each Pixel, with its Syncthing app's device ID.</li>
  <li>Optionally, a Resend API key for alert emails.</li>
</ul>
<p>You won't be asked to invent a password or a key: Darkroom makes and keeps its own.</p>
<p class="text-ink-muted">Once you start, only this browser can continue. After the test sign-in, only you, signed in on this browser, can.</p>
<div class={ROW}>
  <button type="button" class={PRIMARY} disabled={busy} onclick={onstart}>{busy ? "Starting..." : "Start setup"}</button>
</div>
<FormError message={error} />
