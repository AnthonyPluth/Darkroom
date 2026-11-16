<script lang="ts">
  import { untrack } from "svelte";
  import type { SetupAlertsBody, SetupState } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import Field from "../components/Field.svelte";
  import FormError from "../forms/FormError.svelte";
  import { attempt } from "./attempt";
  import EnvRow from "./EnvRow.svelte";
  import { ALERT_VARIABLES, BUTTON, fromEnvironment, PRIMARY, ROW, settingOf, storedValue } from "./wizard";

  let { setup, onsaved, onskip, onback, reload }: {
    setup: SetupState;
    onsaved: (next: SetupState) => void;
    onskip: () => void;
    onback: () => void;
    reload: () => void;
  } = $props();

  let key = $state("");
  let from = $state(untrack(() => storedValue(setup, "ALERT_EMAIL_FROM")) ?? "");
  let to = $state(untrack(() => storedValue(setup, "ALERT_EMAIL_TO")) ?? "");
  let busy = $state(false);
  let error = $state("");
  const inEnvironment = $derived(ALERT_VARIABLES.some((v) => fromEnvironment(setup, v)));
  const keyStored = $derived(settingOf(setup, "RESEND_API_KEY").source === "stored");
  const fromStored = $derived(storedValue(setup, "ALERT_EMAIL_FROM") !== null);
  const toStored = $derived(storedValue(setup, "ALERT_EMAIL_TO") !== null);

  async function save(): Promise<void> {
    if (inEnvironment) return onsaved(setup);
    const body: SetupAlertsBody = { alert_email_from: from.trim(), alert_email_to: to.trim() };
    if (key) body.resend_api_key = key.trim();
    key = "";
    const next = await attempt(() => apiCall<"POST /api/setup/alerts">("/api/setup/alerts", { method: "POST", body }),
      { what: "save the alert settings", busy: (on) => (busy = on), error: (m) => (error = m), outdated: reload });
    if (next) onsaved(next);
  }

  function leave(go: () => void): void {
    error = "";
    go();
  }
</script>

<h1 tabindex="-1" class="text-xl font-bold tracking-tight">Alert emails</h1>
<p>Optional. Darkroom can email one address when something needs you, through Resend. You can skip this and set it in the environment later.</p>
{#if inEnvironment}
  <EnvRow label="Resend API key" />
  <EnvRow label="From address" />
  <EnvRow label="Send alerts to" />
{:else}
  <Field label="Resend API key" hint={keyStored ? "A key is saved. Leave this empty to keep it." : "Darkroom stores it sealed with its secret key and never shows it again."}>
    {#snippet children(field)}
      <input id={field.id} aria-describedby={field.describedby} class="input" name="resend_api_key" type="password" autocomplete="off" maxlength="200" bind:value={key} />
    {/snippet}
  </Field>
  <Field label="From address" hint={fromStored ? "Saved." : "An address on a domain you've verified in Resend."}>
    {#snippet children(field)}
      <input id={field.id} aria-describedby={field.describedby} class="input" name="alert_email_from" type="email" autocomplete="off" maxlength="254" bind:value={from} />
    {/snippet}
  </Field>
  <Field label="Send alerts to" hint={toStored ? "Saved." : undefined}>
    {#snippet children(field)}
      <input id={field.id} aria-describedby={field.describedby} class="input" name="alert_email_to" type="email" autocomplete="off" maxlength="254" bind:value={to} />
    {/snippet}
  </Field>
{/if}
<div class={ROW}>
  <button type="button" class={PRIMARY} disabled={busy} onclick={save}>{inEnvironment ? "Continue" : "Save and continue"}</button>
  <button type="button" class={BUTTON} onclick={() => leave(onskip)}>Skip</button>
  <button type="button" class={BUTTON} onclick={() => leave(onback)}>Back</button>
</div>
<FormError message={error} />
