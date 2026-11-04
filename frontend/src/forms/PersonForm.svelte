<script lang="ts">
  import { untrack } from "svelte";
  import { act } from "$lib/act";
  import type { PersonCreateBody, PersonPatchBody, PersonView } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import Field from "../components/Field.svelte";
  import FormError from "./FormError.svelte";

  let { person, onsaved, oncancel, ondelete }: { person: PersonView | null; onsaved: () => void; oncancel: () => void; ondelete?: () => void } = $props();

  const first = untrack(() => person);
  let name = $state(first?.name ?? "");
  let slug = $state("");
  let email = $state(first?.email ?? "");
  let immichUserId = $state(first?.immich_user_id ?? "");
  let immichLibraryId = $state(first?.immich_library_id ?? "");
  let immichKey = $state("");
  let removeKey = $state(false);
  let busy = $state(false);
  let error = $state("");

  function patch(): PersonPatchBody {
    const body: PersonPatchBody = {
      name,
      email: email.trim() || null,
      immich_user_id: immichUserId.trim() || null,
      immich_library_id: immichLibraryId.trim() || null,
    };
    if (immichKey.trim()) body.immich_api_key = immichKey.trim();
    else if (removeKey) body.immich_api_key = null;
    return body;
  }

  function create(): PersonCreateBody {
    const body: PersonCreateBody = { name };
    if (slug.trim()) body.slug = slug.trim();
    if (email.trim()) body.email = email.trim();
    if (immichUserId.trim()) body.immich_user_id = immichUserId.trim();
    if (immichLibraryId.trim()) body.immich_library_id = immichLibraryId.trim();
    if (immichKey.trim()) body.immich_api_key = immichKey.trim();
    return body;
  }

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    error = "";
    const saved = await act(
      () =>
        first
          ? apiCall<"PATCH /api/people/{id}">(`/api/people/${first.id}`, { method: "PATCH", body: patch() })
          : apiCall<"POST /api/people">("/api/people", { method: "POST", body: create() }),
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
  {#if !first}
    <Field label="Folder name" hint="Lower-case letters, numbers and dashes. Leave empty to build it from the name; it can't be changed later.">
      {#snippet children(field)}
        <input id={field.id} aria-describedby={field.describedby} class="input" name="slug" maxlength="63" bind:value={slug} />
      {/snippet}
    </Field>
  {/if}
  <Field label="Email (optional)">
    {#snippet children(field)}
      <input id={field.id} class="input" name="email" type="email" maxlength="254" bind:value={email} />
    {/snippet}
  </Field>
  <Field label="Immich user ID (optional)">
    {#snippet children(field)}
      <input id={field.id} class="input" name="immich_user_id" maxlength="200" bind:value={immichUserId} />
    {/snippet}
  </Field>
  <Field label="Immich library ID (optional)">
    {#snippet children(field)}
      <input id={field.id} class="input" name="immich_library_id" maxlength="200" bind:value={immichLibraryId} />
    {/snippet}
  </Field>
  <Field
    label="Immich API key"
    hint={first?.has_immich_key ? "A key is saved and is never shown again. Paste a new one to replace it." : "Stored encrypted. Darkroom never shows it again."}
  >
    {#snippet children(field)}
      <input id={field.id} aria-describedby={field.describedby} class="input" name="immich_api_key" type="password" autocomplete="off" maxlength="500" bind:value={immichKey} />
    {/snippet}
  </Field>
  {#if first?.has_immich_key}
    <label class="flex min-h-11 items-center gap-3">
      <input type="checkbox" class="size-5 accent-accent" bind:checked={removeKey} />
      <span>Remove the saved key</span>
    </label>
  {/if}
  <FormError message={error} />
  <div class="flex flex-wrap items-center justify-between gap-3">
    <div>
      {#if first && ondelete}
        <button type="button" class="btn btn-danger" onclick={ondelete}>Delete person</button>
      {/if}
    </div>
    <div class="flex gap-3">
      <button type="button" class="btn" onclick={oncancel}>Cancel</button>
      <button type="submit" class="btn btn-primary" disabled={busy}>{first ? "Save" : "Add person"}</button>
    </div>
  </div>
</form>
