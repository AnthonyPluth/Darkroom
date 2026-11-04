<script lang="ts">
  import { untrack } from "svelte";
  import { act } from "$lib/act";
  import type { AccountView, PersonView } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import Field from "../components/Field.svelte";
  import FormError from "./FormError.svelte";

  let { account, people, onsaved, oncancel, ondelete }: {
    account: AccountView | null;
    people: PersonView[];
    onsaved: () => void;
    oncancel: () => void;
    ondelete?: () => void;
  } = $props();

  const first = untrack(() => account);
  let email = $state(first?.email ?? "");
  let defaultPerson = $state(first?.default_person_id ?? untrack(() => people)[0]?.id ?? 0);
  let busy = $state(false);
  let error = $state("");

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    error = "";
    const body = { email, default_person_id: defaultPerson };
    const saved = await act(
      () =>
        first
          ? apiCall<"PATCH /api/accounts/{id}">(`/api/accounts/${first.id}`, { method: "PATCH", body })
          : apiCall<"POST /api/accounts">("/api/accounts", { method: "POST", body }),
      { busy: (on) => (busy = on), onError: (message) => (error = message) },
    );
    if (saved) onsaved();
  }
</script>

<form class="flex flex-col gap-4" onsubmit={submit}>
  <Field label="Google account email">
    {#snippet children(field)}
      <input id={field.id} class="input" name="email" type="email" required maxlength="254" bind:value={email} />
    {/snippet}
  </Field>
  <Field label="Default person" hint="Google-only photos from this account go to this person.">
    {#snippet children(field)}
      <select id={field.id} aria-describedby={field.describedby} class="input" name="default_person_id" bind:value={defaultPerson}>
        {#each people as person (person.id)}
          <option value={person.id}>{person.name}</option>
        {/each}
      </select>
    {/snippet}
  </Field>
  <FormError message={error} />
  <div class="flex flex-wrap items-center justify-between gap-3">
    <div>
      {#if first && ondelete}
        <button type="button" class="btn btn-danger" onclick={ondelete}>Delete account</button>
      {/if}
    </div>
    <div class="flex gap-3">
      <button type="button" class="btn" onclick={oncancel}>Cancel</button>
      <button type="submit" class="btn btn-primary" disabled={busy}>{first ? "Save" : "Add account"}</button>
    </div>
  </div>
</form>
