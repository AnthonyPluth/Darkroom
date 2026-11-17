<script lang="ts">
  import type { AccountView, PersonView, PixelView } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import Confirm from "../components/Confirm.svelte";
  import Modal from "../components/Modal.svelte";
  import AccountForm from "../forms/AccountForm.svelte";
  import PersonForm from "../forms/PersonForm.svelte";
  import PixelForm from "../forms/PixelForm.svelte";
  import PixelPaired from "../forms/PixelPaired.svelte";
  import PixelPairing from "../forms/PixelPairing.svelte";
  import { formTitle, type Editing, type Family } from "./family";

  type Asking = { title: string; message: string; more?: string[]; confirm: string; primary?: boolean; refused?: (message: string) => string; run: () => Promise<unknown> };

  let { data, onchanged, wide = false }: { data: Family; onchanged: () => void; wide?: boolean } = $props();

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

  export function pair(pixel: PixelView): void {
    edit({ kind: "pairing", item: pixel });
  }

  function added(pixel: PixelView): void {
    editing = { kind: "pairing", item: pixel };
    onchanged();
  }

  function paired(pixel: PixelView): void {
    editing = { kind: "paired", item: pixel };
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

  export function shareAgain(pixel: PixelView): void {
    ask({
      title: `Share ${pixel.name}'s folder again?`,
      message: `Darkroom removed ${pixel.name}'s folder from its Syncthing because the folder was changed outside Darkroom: it was no longer Send Only, or no longer at ${pixel.name}'s folder on the photo share.`,
      more: [
        `If you share it again, Darkroom adds the folder afresh, Send Only, at ${pixel.name}'s folder, as soon as the photo share passes its check and the folder is not held. If the Syncthing app on ${pixel.name} asks, accept the folder there.`,
        "If the folder is changed outside Darkroom again, Darkroom removes it again.",
      ],
      confirm: "Share folder again",
      primary: true,
      refused: (m) => `Couldn't share ${pixel.name}'s folder again: ${m.replace(/\.$/, "")}.`,
      run: () => apiCall<"POST /api/pixels/{id}/release-folder">(`/api/pixels/${pixel.id}/release-folder`, { method: "POST", body: { confirmed: true } }),
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

<Modal bind:open={formOpen} {wide} title={editing ? formTitle(editing) : ""} description={editing?.kind === "pairing" ? `Connect the Syncthing app on ${editing.item.name} to Darkroom. Nothing is shared until you approve the Pixel here.` : undefined}>
  {#if editing?.kind === "person"}
    {@const person = editing.item}
    <PersonForm {person} onsaved={saved} oncancel={() => (formOpen = false)} ondelete={person ? () => removePerson(person) : undefined} />
  {:else if editing?.kind === "account"}
    {@const account = editing.item}
    <AccountForm {account} people={data.people} onsaved={saved} oncancel={() => (formOpen = false)} ondelete={account ? () => removeAccount(account) : undefined} />
  {:else if editing?.kind === "pixel"}
    {@const pixel = editing.item}
    <PixelForm {pixel} accounts={data.accounts} people={data.people} onsaved={saved} oncancel={() => (formOpen = false)} ondelete={pixel ? () => removePixel(pixel) : undefined}
      pairing={data.transport?.pairing ?? false} onadded={data.transport ? added : undefined} onpair={data.transport && pixel ? () => pair(pixel) : undefined} />
  {:else if editing?.kind === "pairing"}
    <PixelPairing pixel={editing.item} onpaired={paired} onlater={() => (formOpen = false)} />
  {:else if editing?.kind === "paired"}
    <PixelPaired name={editing.item.name} ondone={() => (formOpen = false)} />
  {/if}
</Modal>

{#if asking}
  <Confirm bind:open={confirmOpen} title={asking.title} message={asking.message} more={asking.more} confirm={asking.confirm} primary={asking.primary} refused={asking.refused} {wide} run={asking.run} done={removed} />
{/if}
