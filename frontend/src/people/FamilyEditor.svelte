<script lang="ts">
  import type { AccountView, PersonView, PixelView } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import Confirm from "../components/Confirm.svelte";
  import Modal from "../components/Modal.svelte";
  import AccountForm from "../forms/AccountForm.svelte";
  import PersonForm from "../forms/PersonForm.svelte";
  import PixelForm from "../forms/PixelForm.svelte";
  import { formTitle, type Editing, type Family } from "./family";

  type Asking = { title: string; message: string; confirm: string; run: () => Promise<unknown> };

  let { data, onchanged }: { data: Family; onchanged: () => void } = $props();

  let formOpen = $state(false);
  let editing = $state<Editing | null>(null);
  let confirmOpen = $state(false);
  let asking = $state<Asking | null>(null);

  export function edit(next: Editing): void {
    editing = next;
    formOpen = true;
  }

  function saved(): void {
    formOpen = false;
    onchanged();
  }

  function ask(next: Asking): void {
    formOpen = false;
    asking = next;
    confirmOpen = true;
  }

  function removed(): void {
    confirmOpen = false;
    onchanged();
  }

  function removePerson(person: PersonView): void {
    ask({
      title: `Delete ${person.name}?`,
      message: "Only a person with no photos, Google accounts or Pixels can be deleted. Their folders are never touched.",
      confirm: "Delete person",
      run: () => apiCall<"DELETE /api/people/{id}">(`/api/people/${person.id}`, { method: "DELETE" }),
    });
  }

  function removeAccount(account: AccountView): void {
    ask({
      title: `Delete ${account.email}?`,
      message: "Only an account with no Pixels and no exports can be deleted.",
      confirm: "Delete account",
      run: () => apiCall<"DELETE /api/accounts/{id}">(`/api/accounts/${account.id}`, { method: "DELETE" }),
    });
  }

  function removePixel(pixel: PixelView): void {
    ask({
      title: `Delete ${pixel.name}?`,
      message: "Photos already backed up stay in Google Photos. Nothing is deleted from Darkroom's folders.",
      confirm: "Delete Pixel",
      run: () => apiCall<"DELETE /api/pixels/{id}">(`/api/pixels/${pixel.id}`, { method: "DELETE" }),
    });
  }
</script>

<Modal bind:open={formOpen} title={editing ? formTitle(editing) : ""}>
  {#if editing?.kind === "person"}
    {@const person = editing.item}
    <PersonForm {person} onsaved={saved} oncancel={() => (formOpen = false)} ondelete={person ? () => removePerson(person) : undefined} />
  {:else if editing?.kind === "account"}
    {@const account = editing.item}
    <AccountForm {account} people={data.people} onsaved={saved} oncancel={() => (formOpen = false)} ondelete={account ? () => removeAccount(account) : undefined} />
  {:else if editing?.kind === "pixel"}
    {@const pixel = editing.item}
    <PixelForm {pixel} accounts={data.accounts} people={data.people} onsaved={saved} oncancel={() => (formOpen = false)} ondelete={pixel ? () => removePixel(pixel) : undefined} />
  {/if}
</Modal>

{#if asking}
  <Confirm bind:open={confirmOpen} title={asking.title} message={asking.message} confirm={asking.confirm} run={asking.run} done={removed} />
{/if}
