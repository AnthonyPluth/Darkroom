<script lang="ts">
  import { untrack } from "svelte";
  import type { SetupState } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import Field from "../components/Field.svelte";
  import FormError from "../forms/FormError.svelte";
  import { attempt } from "./attempt";
  import EnvRow from "./EnvRow.svelte";
  import { fromEnvironment, PRIMARY, ROW, storedValue } from "./wizard";

  let { setup, works, onsaved, reload }: { setup: SetupState; works: string | null; onsaved: (next: SetupState) => void; reload: () => void } = $props();

  let address = $state(untrack(() => storedValue(setup, "IMMICH_URL")) ?? "");
  let busy = $state(false);
  let error = $state("");
  const inEnvironment = $derived(fromEnvironment(setup, "IMMICH_URL"));
  const stored = $derived(storedValue(setup, "IMMICH_URL") !== null);

  async function save(): Promise<void> {
    if (inEnvironment) return onsaved(setup);
    const next = await attempt(() => apiCall<"POST /api/setup/immich">("/api/setup/immich", { method: "POST", body: { immich_url: address.trim() } }),
      { what: "save Immich's address", busy: (on) => (busy = on), error: (m) => (error = m), outdated: reload });
    if (next) onsaved(next);
  }
</script>

{#if works !== null}
  <p role="status" class="flex items-start gap-2 rounded-md bg-ok-bg px-3 py-2.5 font-semibold text-ok"><span class="mt-1.5 block size-2 flex-none rounded-full bg-ok"></span><span>{works}</span></p>
{/if}
<h1 tabindex="-1" class="text-xl font-bold tracking-tight">Immich</h1>
<p>The address Darkroom reaches Immich at from this server, such as <span class="font-mono text-sm">http://192.168.1.20:2283</span>.</p>
{#if inEnvironment}
  <EnvRow label="Immich address" />
{:else}
  <Field label="Immich address" hint={stored ? "Saved." : undefined}>
    {#snippet children(field)}
      <input id={field.id} aria-describedby={field.describedby} class="input" name="immich_url" type="url" autocomplete="off" maxlength="500" bind:value={address} />
    {/snippet}
  </Field>
{/if}
<div class={ROW}>
  <button type="button" class={PRIMARY} disabled={busy} onclick={save}>{inEnvironment ? "Continue" : busy ? "Checking Immich..." : "Save and continue"}</button>
</div>
<FormError message={error} />
