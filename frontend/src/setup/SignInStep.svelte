<script lang="ts">
  import { untrack } from "svelte";
  import type { SetupSignInBody, SetupState } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import Field from "../components/Field.svelte";
  import FormError from "../forms/FormError.svelte";
  import { attempt } from "./attempt";
  import EnvRow from "./EnvRow.svelte";
  import Saved from "./Saved.svelte";
  import { browser, BUTTON, COPY_FAILED, copyText, selectText, fromEnvironment, joinList, PRIMARY, ROW, splitList, startTesting } from "./wizard";

  let { setup, onstate, onback, goAddress, reload }: {
    setup: SetupState;
    onstate: (next: SetupState) => void;
    onback: () => void;
    goAddress: () => void;
    reload: () => void;
  } = $props();

  type Fields = { issuer: string; clientId: string; groups: string; emails: string };
  const fieldsOf = (s: SetupState): Fields => ({
    issuer: s.proposal?.oidc_issuer ?? "",
    clientId: s.proposal?.oidc_client_id ?? "",
    groups: joinList(s.proposal?.oidc_allowed_groups ?? []),
    emails: joinList(s.proposal?.oidc_allowed_emails ?? []),
  });

  let fields = $state(untrack(() => fieldsOf(setup)));
  let saved = $state(untrack(() => fieldsOf(setup)));
  let secret = $state("");
  let busy = $state(false);
  let saveError = $state("");
  let savedNow = $state(false);
  let testError = $state("");
  let copied = $state(false);
  let copyFailed = $state(false);
  let callbackBox: HTMLElement | undefined = $state();

  const env = $derived({
    issuer: fromEnvironment(setup, "OIDC_ISSUER"),
    clientId: fromEnvironment(setup, "OIDC_CLIENT_ID"),
    secret: fromEnvironment(setup, "OIDC_CLIENT_SECRET"),
    groups: fromEnvironment(setup, "OIDC_ALLOWED_GROUPS"),
    emails: fromEnvironment(setup, "OIDC_ALLOWED_EMAILS"),
  });
  const same = (a: Fields, b: Fields): boolean =>
    a.issuer.trim() === b.issuer.trim() && a.clientId.trim() === b.clientId.trim()
    && joinList(splitList(a.groups)) === joinList(splitList(b.groups)) && joinList(splitList(a.emails)) === joinList(splitList(b.emails));
  const testable = $derived(setup.proposal !== null && secret === "" && same(fields, saved));

  function clear(): void {
    saveError = "";
    testError = "";
    copied = false;
    copyFailed = false;
  }

  async function copy(): Promise<void> {
    clear();
    if (!setup.redirect_uri) return;
    copied = await copyText(setup.redirect_uri);
    copyFailed = !copied;
    if (copyFailed && callbackBox) selectText(callbackBox);
  }

  function body(): SetupSignInBody {
    const sent: SetupSignInBody = {
      oidc_issuer: fields.issuer.trim(),
      oidc_client_id: fields.clientId.trim(),
      oidc_allowed_groups: splitList(fields.groups),
      oidc_allowed_emails: splitList(fields.emails),
    };
    if (secret) sent.oidc_client_secret = secret;
    return sent;
  }

  async function save(): Promise<void> {
    clear();
    savedNow = false;
    const sent = body();
    secret = "";
    const next = await attempt(() => apiCall<"POST /api/setup/sign-in">("/api/setup/sign-in", { method: "POST", body: sent }),
      { what: "save the sign-in details", busy: (on) => (busy = on), error: (m) => (saveError = m), outdated: reload });
    if (!next) return;
    saved = fieldsOf(next);
    fields = fieldsOf(next);
    savedNow = true;
    onstate(next);
  }

  async function test(): Promise<void> {
    clear();
    savedNow = false;
    const started = await attempt(() => apiCall<"POST /api/setup/sign-in/test">("/api/setup/sign-in/test", { method: "POST" }),
      { what: "start the test sign-in", busy: (on) => (busy = on), error: (m) => (testError = m) });
    if (!started) return;
    startTesting();
    browser.leave(started.location);
  }

  function back(): void {
    clear();
    onback();
  }
</script>

<h1 tabindex="-1" class="text-xl font-bold tracking-tight">Sign-in</h1>
<div class="flex flex-col gap-3">
  <h2 class="text-md font-bold">1. Add Darkroom to Pocket ID</h2>
  {#if setup.redirect_uri}
    <p>In Pocket ID, add an OIDC client for Darkroom with this callback URL:</p>
    <div class="rounded-md border border-line bg-canvas px-3 py-2.5 font-mono text-sm break-all" aria-label="Callback URL" bind:this={callbackBox}>{setup.redirect_uri}</div>
    <div class={ROW}>
      <button type="button" class={BUTTON} onclick={copy}>Copy</button>
    </div>
    {#if copied}<Saved message="Copied." />{/if}
    <FormError message={copyFailed ? COPY_FAILED : ""} />
  {:else}
    <p>Confirm Darkroom's address first. <a class="font-semibold underline" href="/setup" onclick={(e) => { e.preventDefault(); clear(); goAddress(); }}>Go to Address</a></p>
  {/if}
</div>
<div class="flex flex-col gap-3 border-t border-raised pt-4 md:pt-5">
  <h2 class="text-md font-bold">2. Enter the client's details</h2>
  {#if env.issuer}
    <EnvRow label="Issuer address" />
  {:else}
    <Field label="Issuer address" hint="Pocket ID's address, such as https://id.example.com. Use https:// unless Pocket ID is on your home network.">
      {#snippet children(field)}
        <input id={field.id} aria-describedby={field.describedby} class="input" name="oidc_issuer" type="url" autocomplete="off" maxlength="500" bind:value={fields.issuer} />
      {/snippet}
    </Field>
  {/if}
  {#if env.clientId}
    <EnvRow label="Client ID" />
  {:else}
    <Field label="Client ID">
      {#snippet children(field)}
        <input id={field.id} class="input" name="oidc_client_id" autocomplete="off" maxlength="200" bind:value={fields.clientId} />
      {/snippet}
    </Field>
  {/if}
  {#if env.secret}
    <EnvRow label="Client secret" />
  {:else}
    <Field label="Client secret" hint={setup.proposal?.has_client_secret ? "A secret is saved. Leave this empty to keep it." : undefined}>
      {#snippet children(field)}
        <input id={field.id} aria-describedby={field.describedby} class="input" name="oidc_client_secret" type="password" autocomplete="off" maxlength="500" bind:value={secret} />
      {/snippet}
    </Field>
  {/if}
  {#if env.groups}
    <EnvRow label="Allowed groups" />
  {:else}
    <Field label="Allowed groups" hint="People in any of these Pocket ID groups may sign in. Separate them with commas.">
      {#snippet children(field)}
        <input id={field.id} aria-describedby={field.describedby} class="input" name="oidc_allowed_groups" autocomplete="off" bind:value={fields.groups} />
      {/snippet}
    </Field>
  {/if}
  {#if env.emails}
    <EnvRow label="Allowed email addresses" />
  {:else}
    <Field label="Allowed email addresses" hint="Or these people, by email. Separate them with commas. Set at least one group or address.">
      {#snippet children(field)}
        <input id={field.id} aria-describedby={field.describedby} class="input" name="oidc_allowed_emails" autocomplete="off" bind:value={fields.emails} />
      {/snippet}
    </Field>
  {/if}
  <div class={ROW}>
    <button type="button" class={BUTTON} disabled={busy} onclick={save}>Save details</button>
  </div>
  {#if savedNow}<Saved message="Saved. Test them below." />{/if}
  <FormError message={saveError} />
</div>
<div class="flex flex-col gap-3 border-t border-raised pt-4 md:pt-5">
  <h2 class="text-md font-bold">3. Test the sign-in</h2>
  <p>Sign in with your own account, one these details allow. If it works, Darkroom keeps these details, you stay signed in, and setup carries on with Immich. Only you, on this browser, can continue it from then on.</p>
  <div class={ROW}>
    <button type="button" class={PRIMARY} disabled={busy || !testable} aria-describedby={testable ? undefined : "sign-in-why"} onclick={test}>Test sign-in</button>
    <button type="button" class={BUTTON} onclick={back}>Back</button>
  </div>
  {#if !testable}<p id="sign-in-why" class="text-ink-muted">Save the details above first.</p>{/if}
  <FormError message={testError} />
</div>
