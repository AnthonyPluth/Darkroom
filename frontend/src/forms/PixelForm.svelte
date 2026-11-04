<script lang="ts">
  import { untrack } from "svelte";
  import { act } from "$lib/act";
  import type { AccountView, PersonView, PixelView } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import Field from "../components/Field.svelte";
  import FormError from "./FormError.svelte";

  let { pixel, accounts, people, onsaved, oncancel, ondelete }: {
    pixel: PixelView | null;
    accounts: AccountView[];
    people: PersonView[];
    onsaved: () => void;
    oncancel: () => void;
    ondelete?: () => void;
  } = $props();

  const first = untrack(() => pixel);
  const known = untrack(() => accounts);
  const defaultOf = (accountId: number): number[] => {
    const found = known.find((a) => a.id === accountId);
    return found ? [found.default_person_id] : [];
  };

  let name = $state(first?.name ?? "");
  let syncthing = $state(first?.syncthing_device_id ?? "");
  let accountId = $state(first?.google_account_id ?? known[0]?.id ?? 0);
  let served = $state<number[]>(untrack(() => (first ? first.people_ids : defaultOf(accountId))));
  let chosen = $state(first !== null);
  let busy = $state(false);
  let error = $state("");

  function pickAccount(): void {
    if (!chosen) served = defaultOf(accountId);
  }

  function toggle(personId: number, on: boolean): void {
    chosen = true;
    served = on ? [...served, personId] : served.filter((id) => id !== personId);
  }

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    error = "";
    if (served.length === 0) {
      error = "Choose at least one person this Pixel serves.";
      return;
    }
    const body = { name, google_account_id: accountId, syncthing_device_id: syncthing.trim() || null, people_ids: [...served].sort((a, b) => a - b) };
    const saved = await act(
      () =>
        first
          ? apiCall<"PATCH /api/pixels/{id}">(`/api/pixels/${first.id}`, { method: "PATCH", body })
          : apiCall<"POST /api/pixels">("/api/pixels", { method: "POST", body }),
      { busy: (on) => (busy = on), onError: (message) => (error = message) },
    );
    if (saved) onsaved();
  }
</script>

<form class="flex flex-col gap-4" onsubmit={submit}>
  <Field label="Name">
    {#snippet children(field)}
      <input id={field.id} class="input" name="name" required maxlength="100" bind:value={name} />
    {/snippet}
  </Field>
  <Field label="Syncthing device ID (optional)">
    {#snippet children(field)}
      <input id={field.id} class="input font-mono" name="syncthing_device_id" maxlength="80" bind:value={syncthing} />
    {/snippet}
  </Field>
  <Field label="Google account" hint="Everything this Pixel uploads lands in this account.">
    {#snippet children(field)}
      <select id={field.id} aria-describedby={field.describedby} class="input" name="google_account_id" bind:value={accountId} onchange={pickAccount}>
        {#each accounts as account (account.id)}
          <option value={account.id}>{account.email}</option>
        {/each}
      </select>
    {/snippet}
  </Field>
  <fieldset class="flex flex-col gap-1">
    <legend class="mb-1 font-semibold">People served</legend>
    {#each people as person (person.id)}
      <label class="flex min-h-11 items-center gap-3">
        <input type="checkbox" class="size-5 accent-accent" checked={served.includes(person.id)} onchange={(e) => toggle(person.id, e.currentTarget.checked)} />
        <span>{person.name}</span>
      </label>
    {/each}
    <p class="text-sm text-ink-muted">A Pixel serves one person by default. Tick more for a shared Pixel; each person's folder must then be turned on for backup in Google Photos there.</p>
  </fieldset>
  <FormError message={error} />
  <div class="flex flex-wrap items-center justify-between gap-3">
    <div>
      {#if first && ondelete}
        <button type="button" class="btn btn-danger" onclick={ondelete}>Delete Pixel</button>
      {/if}
    </div>
    <div class="flex gap-3">
      <button type="button" class="btn" onclick={oncancel}>Cancel</button>
      <button type="submit" class="btn btn-primary" disabled={busy}>{first ? "Save" : "Add Pixel"}</button>
    </div>
  </div>
</form>
