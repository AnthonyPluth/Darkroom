<script lang="ts">
  import { untrack } from "svelte";
  import type { SetupState } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import Field from "../components/Field.svelte";
  import FormError from "../forms/FormError.svelte";
  import { attempt } from "./attempt";
  import EnvRow from "./EnvRow.svelte";
  import { BUTTON, fromEnvironment, PRIMARY, ROW, storedValue } from "./wizard";

  let { setup, onsaved, onback, reload }: { setup: SetupState; onsaved: (next: SetupState) => void; onback: () => void; reload: () => void } = $props();

  const HINT = "This is the address you're using now. The test sign-in in the next step can only come back here, so keep it for setup. To use an https:// address through Traefik later, set DARKROOM_PUBLIC_URL and the OIDC_ settings in the environment then.";
  const here = location.origin;
  let address = $state(untrack(() => storedValue(setup, "DARKROOM_PUBLIC_URL")) ?? here);
  let busy = $state(false);
  let error = $state("");
  const inEnvironment = $derived(fromEnvironment(setup, "DARKROOM_PUBLIC_URL"));
  const elsewhere = $derived(address.trim().replace(/\/+$/, "") !== here);
  const stored = $derived(storedValue(setup, "DARKROOM_PUBLIC_URL") !== null);

  async function confirm(): Promise<void> {
    if (inEnvironment) return onsaved(setup);
    const next = await attempt(() => apiCall<"POST /api/setup/address">("/api/setup/address", { method: "POST", body: { public_url: address.trim() } }),
      { what: "save the address", busy: (on) => (busy = on), error: (m) => (error = m), outdated: reload });
    if (next) onsaved(next);
  }

  function back(): void {
    error = "";
    onback();
  }
</script>

<h1 tabindex="-1" class="text-xl font-bold tracking-tight">Darkroom's address</h1>
<p>The address people open Darkroom at. Sign-in comes back to it, and alert emails link to it.</p>
{#if inEnvironment}
  <EnvRow label="Address" />
{:else}
  <Field label="Address" hint={stored ? "Saved." : HINT}>
    {#snippet children(field)}
      <input id={field.id} aria-describedby={field.describedby} class="input" name="public_url" type="url" autocomplete="off" maxlength="500" bind:value={address} />
      {#if elsewhere}
        <p class="order-last flex items-start gap-2 text-sm text-warn"><span class="mt-1.25 block size-2 flex-none rotate-45 bg-warn"></span>This isn't the address you're using now, so the test sign-in in the next step can't finish here.</p>
      {/if}
    {/snippet}
  </Field>
{/if}
<div class={ROW}>
  <button type="button" class={PRIMARY} disabled={busy} onclick={confirm}>{inEnvironment ? "Continue" : "Confirm address"}</button>
  <button type="button" class={BUTTON} onclick={back}>Back</button>
</div>
<FormError message={error} />
