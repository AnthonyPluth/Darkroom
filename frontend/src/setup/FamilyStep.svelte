<script lang="ts">
  import LoadFailed from "../components/LoadFailed.svelte";
  import AccountTable from "../people/AccountTable.svelte";
  import type { Family } from "../people/family";
  import FamilyEditor from "../people/FamilyEditor.svelte";
  import PersonTable from "../people/PersonTable.svelte";
  import PixelTable from "../people/PixelTable.svelte";
  import { BUTTON, PRIMARY, ROW } from "./wizard";

  let { part, data, failed, retry, onchanged, oncontinue, onskip, onback }: {
    part: "people" | "accounts" | "pixels";
    data: Family;
    failed: string;
    retry: () => void;
    onchanged: () => void;
    oncontinue: () => void;
    onskip: () => void;
    onback: () => void;
  } = $props();

  let editor: FamilyEditor | undefined = $state();
  const none = $derived(part === "people" ? data.people.length === 0 : part === "accounts" ? data.accounts.length === 0 : data.pixels.length === 0);
</script>

{#if part === "people"}
  <h1 tabindex="-1" class="text-xl font-bold tracking-tight">People</h1>
  <p>Add each person whose photos Darkroom looks after, with an Immich API key from their own Immich account.</p>
{:else if part === "accounts"}
  <h1 tabindex="-1" class="text-xl font-bold tracking-tight">Google accounts</h1>
  <p>Add each Google account whose Google Photos Darkroom exports, and sign it in once in the remote browser. Darkroom never sees the password; you type it into Google yourself.</p>
{:else}
  <h1 tabindex="-1" class="text-xl font-bold tracking-tight">Pixels</h1>
  <p>Add each Pixel that uploads to Google Photos, the Google account it uploads to and the people whose photos it carries.</p>
{/if}
{#if failed}
  <LoadFailed message={failed} {retry} />
{:else if part === "people"}
  <div class="flex items-center justify-between gap-3">
    <h2 class="text-md font-bold">Persons</h2>
    <button type="button" class="btn" onclick={() => editor?.edit({ kind: "person", item: null })}>Add person</button>
  </div>
  <PersonTable {data} empty="No people yet. Add at least one person to continue." onedit={(item) => editor?.edit({ kind: "person", item })} />
{:else if part === "accounts"}
  <div class={ROW}>
    <button type="button" class={BUTTON} disabled={data.people.length === 0} onclick={() => editor?.edit({ kind: "account", item: null })}>Add account</button>
  </div>
  <AccountTable {data} empty="No Google accounts yet. You can add them now or later on the People page." signIn={{ href: "/browser?from=setup" }} onedit={(item) => editor?.edit({ kind: "account", item })} />
{:else}
  <div class={ROW}>
    <button type="button" class={BUTTON} disabled={data.accounts.length === 0} onclick={() => editor?.edit({ kind: "pixel", item: null })}>Add Pixel</button>
  </div>
  <PixelTable {data} empty="No Pixels yet. You can add them now or later on the People page." onedit={(item) => editor?.edit({ kind: "pixel", item })} />
{/if}
<div class={ROW}>
  <button type="button" class={PRIMARY} disabled={Boolean(failed) || (part === "people" && none)} onclick={oncontinue}>Continue</button>
  {#if !failed && part !== "people" && none}
    <button type="button" class={BUTTON} onclick={onskip}>Skip for now</button>
  {/if}
  <button type="button" class={BUTTON} onclick={onback}>Back</button>
</div>

<FamilyEditor bind:this={editor} {data} {onchanged} />
