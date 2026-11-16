<script lang="ts">
  import type { SetupState } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import LoadFailed from "../components/LoadFailed.svelte";
  import FormError from "../forms/FormError.svelte";
  import { attempt } from "./attempt";
  import { alertsOn, BUTTON, PRIMARY, ROW, type Counts } from "./wizard";

  let { setup, counts, failed, retry, onfinished, onback }: {
    setup: SetupState;
    counts: Counts | null;
    failed: string;
    retry: () => void;
    onfinished: () => void;
    onback: () => void;
  } = $props();

  let busy = $state(false);
  let error = $state("");

  async function finish(): Promise<void> {
    const done = await attempt(() => apiCall<"POST /api/setup/finish">("/api/setup/finish", { method: "POST" }),
      { what: "finish setup", busy: (on) => (busy = on), error: (m) => (error = m) });
    if (done) onfinished();
  }

  function back(): void {
    error = "";
    onback();
  }
</script>

<h1 tabindex="-1" class="text-xl font-bold tracking-tight">Darkroom is set up</h1>
{#if failed || !counts}
  <LoadFailed message={failed} {retry} />
{:else}
  <p>{counts.people} people, {counts.accounts} Google accounts and {counts.pixels} Pixels. Alert emails: {alertsOn(setup) ? "on" : "off"}.</p>
{/if}
<h2 class="text-md font-bold">Before you go</h2>
<ol class="flex list-decimal flex-col gap-1.5 pl-5">
  <li>Remove <code class="font-mono text-sm">DARKROOM_WIZARD=1</code> from the container's settings.</li>
  <li>Back up the appdata folder. It holds Darkroom's database and its secret key, which are only useful together, so keep the backup as safe as a password.</li>
  <li>Google accounts still waiting for their sign-in are listed on the People page; sign them in from there.</li>
</ol>
<p class="text-ink-muted">Finishing closes setup for good. After that, settings are changed in Darkroom's environment.</p>
<div class={ROW}>
  <button type="button" class={PRIMARY} disabled={busy} onclick={finish}>Finish setup</button>
  <button type="button" class={BUTTON} onclick={back}>Back</button>
</div>
<FormError message={error} />
