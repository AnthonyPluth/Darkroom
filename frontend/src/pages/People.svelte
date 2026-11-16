<script lang="ts">
  import { onMount } from "svelte";
  import { errMsg } from "$lib/act";
  import type { AccountView, PersonView, PixelView } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import { ago, names, nowSeconds } from "$lib/format";
  import { Loader } from "$lib/loader.svelte";
  import { followLink } from "$lib/nav.svelte";
  import { pairingMode, sentence } from "$lib/pairing";
  import { sessionChip } from "$lib/view";
  import Chip from "../components/Chip.svelte";
  import Confirm from "../components/Confirm.svelte";
  import LoadFailed from "../components/LoadFailed.svelte";
  import Modal from "../components/Modal.svelte";
  import Table from "../components/Table.svelte";
  import AccountForm from "../forms/AccountForm.svelte";
  import PersonForm from "../forms/PersonForm.svelte";
  import PixelForm from "../forms/PixelForm.svelte";
  import PixelPaired from "../forms/PixelPaired.svelte";
  import PixelPairing from "../forms/PixelPairing.svelte";

  type Editing =
    | { kind: "person"; item: PersonView | null }
    | { kind: "account"; item: AccountView | null }
    | { kind: "pixel"; item: PixelView | null }
    | { kind: "pairing"; item: PixelView }
    | { kind: "paired"; item: PixelView };
  type Asking = { title: string; message: string; detail?: string; confirm: string; run: () => Promise<unknown> };

  const loader = new Loader(async () => {
    const [people, accounts, pixels, pairing] = await Promise.all([
      apiCall<"GET /api/people">("/api/people", { failed: "Couldn't load the people" }),
      apiCall<"GET /api/accounts">("/api/accounts", { failed: "Couldn't load the Google accounts" }),
      apiCall<"GET /api/pixels">("/api/pixels", { failed: "Couldn't load the Pixels" }),
      pairingMode(),
    ]);
    return { people: people.people, accounts: accounts.accounts, pixels: pixels.pixels, pairing: pairing.kind === "on", now: nowSeconds() };
  });

  let formOpen = $state(false);
  let editing = $state<Editing | null>(null);
  let confirmOpen = $state(false);
  let asking = $state<Asking | null>(null);

  onMount(() => void loader.load());

  function edit(next: Editing): void {
    editing = next;
    formOpen = true;
  }

  function saved(): void {
    formOpen = false;
    void loader.load(true);
  }

  function pair(pixel: PixelView): void {
    edit({ kind: "pairing", item: pixel });
  }

  function added(pixel: PixelView): void {
    editing = { kind: "pairing", item: pixel };
    void loader.load(true);
  }

  function paired(pixel: PixelView): void {
    editing = { kind: "paired", item: pixel };
    void loader.load(true);
  }

  function ask(next: Asking): void {
    formOpen = false;
    asking = next;
    confirmOpen = true;
  }

  function removed(): void {
    confirmOpen = false;
    void loader.load(true);
  }

  const personName = (people: PersonView[], id: number): string => people.find((p) => p.id === id)?.name ?? "Unknown";

  function pixelLabel(pixel: PixelView, person: PersonView, people: PersonView[]): string {
    const others = pixel.people_ids.filter((id) => id !== person.id).map((id) => personName(people, id));
    return others.length ? `${pixel.name} (also ${others.join(", ")})` : pixel.name;
  }

  function formTitle(next: Editing): string {
    if (next.kind === "pairing" || next.kind === "paired") return `Pair ${next.item.name}`;
    const noun = next.kind === "person" ? "person" : next.kind === "account" ? "Google account" : "Pixel";
    return next.item ? `Edit ${noun}` : `Add ${noun}`;
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

  function unpairPixel(pixel: PixelView): void {
    ask({
      title: `Unpair ${pixel.name}?`,
      message: `Darkroom stops sharing ${pixel.name}'s folder with this device and removes the device from its Syncthing within a minute. Nothing new reaches ${pixel.name} until it is paired again.`,
      detail: `No photo or file is deleted. Darkroom's copies stay on the photo share, and Darkroom does not touch the copies already synced to ${pixel.name}: they stay on the Pixel until someone removes them there.`,
      confirm: "Unpair",
      run: async () => {
        try {
          return await apiCall<"PATCH /api/pixels/{id}">(`/api/pixels/${pixel.id}`, { method: "PATCH", body: { syncthing_device_id: null } });
        } catch (err) {
          throw new Error(`Couldn't unpair ${pixel.name}: ${sentence(errMsg(err))}`, { cause: err });
        }
      },
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

<div class="flex flex-col gap-7">
  <div class="flex flex-col gap-1">
    <h1 class="text-xl font-bold tracking-tight">People</h1>
    <p class="text-ink-muted">Who the family is, and which Google accounts and Pixels serve them.</p>
  </div>

  {#if loader.current.kind === "loading"}
    <p role="status" class="text-ink-muted">Loading...</p>
  {:else if loader.current.kind === "error"}
    <LoadFailed message={loader.current.message} retry={() => loader.load()} />
  {:else}
    {@const data = loader.current.data}
    <section class="card" aria-label="Persons">
      <div class="flex items-center justify-between gap-3">
        <h2 class="text-md font-semibold">Persons</h2>
        <button type="button" class="btn" onclick={() => edit({ kind: "person", item: null })}>Add person</button>
      </div>
      <Table label="Persons" columns={["Person", "Immich user", "Google account", "Pixel", ""]} rows={data.people} empty="No one has been added yet.">
        {#snippet row(person)}
          <tr>
            <td data-label="Person"><b>{person.name}</b></td>
            <td data-label="Immich user">{person.immich_user_id ?? "Not linked"}<br /><span class="text-ink-muted">{person.has_immich_key ? "API key saved" : "No API key"}</span></td>
            <td data-label="Google account">{names(data.accounts.filter((a) => a.served_people.includes(person.id)).map((a) => a.email), "None")}</td>
            <td data-label="Pixel">{names(data.pixels.filter((p) => p.people_ids.includes(person.id)).map((p) => pixelLabel(p, person, data.people)), "None")}</td>
            <td class="md:text-right"><button type="button" class="btn" aria-label="Edit {person.name}" onclick={() => edit({ kind: "person", item: person })}>Edit</button></td>
          </tr>
        {/snippet}
      </Table>
    </section>

    <section class="card" aria-label="Google accounts">
      <div class="flex items-center justify-between gap-3">
        <h2 class="text-md font-semibold">Google accounts</h2>
        <button type="button" class="btn" disabled={data.people.length === 0} onclick={() => edit({ kind: "account", item: null })}>Add account</button>
      </div>
      <Table label="Google accounts" columns={["Account", "Default person", "Serves", "Session", ""]} rows={data.accounts} empty={data.people.length === 0 ? "Add a person first." : "No Google accounts yet."}>
        {#snippet row(account)}
          {@const chip = sessionChip(account.session_state)}
          <tr>
            <td data-label="Account">{account.email}</td>
            <td data-label="Default person">{personName(data.people, account.default_person_id)}</td>
            <td data-label="Serves">{names(account.served_people.map((id) => personName(data.people, id)), "No one")}</td>
            <td data-label="Session"><Chip tone={chip.tone} label={chip.label} /></td>
            <td class="md:text-right">
              <span class="flex flex-wrap justify-end gap-2">
                {#if account.session_state === "needs_sign_in"}
                  <a class="btn btn-primary" href="/browser" onclick={(e) => followLink(e, "/browser")}>Sign in</a>
                {/if}
                <button type="button" class="btn" aria-label="Edit {account.email}" onclick={() => edit({ kind: "account", item: account })}>Edit</button>
              </span>
            </td>
          </tr>
        {/snippet}
      </Table>
    </section>

    <section class="card" aria-label="Pixels">
      <div class="flex items-center justify-between gap-3">
        <h2 class="text-md font-semibold">Pixels</h2>
        <button type="button" class="btn" disabled={data.accounts.length === 0} onclick={() => edit({ kind: "pixel", item: null })}>Add Pixel</button>
      </div>
      <Table label="Pixels" columns={["Device", "Google account", "People served", "Last sync", ""]} rows={data.pixels} empty={data.accounts.length === 0 ? "Add a Google account first." : "No Pixels yet."}>
        {#snippet row(pixel)}
          <tr>
            <td data-label="Device"><b>{pixel.name}</b></td>
            <td data-label="Google account">{data.accounts.find((a) => a.id === pixel.google_account_id)?.email ?? "Unknown"}</td>
            <td data-label="People served">{names(pixel.people_ids.map((id) => personName(data.people, id)), "No one")}</td>
            <td data-label="Last sync" class:text-ink-muted={pixel.last_sync === null}>{pixel.last_sync === null ? "Not synced yet" : ago(pixel.last_sync, data.now)}</td>
            <td class="md:text-right">
              <span class="flex flex-wrap justify-end gap-2">
                {#if data.pairing && !pixel.syncthing_device_id}
                  <button type="button" class="btn" aria-label="Pair {pixel.name}" onclick={() => pair(pixel)}>Pair</button>
                {/if}
                <button type="button" class="btn" aria-label="Edit {pixel.name}" onclick={() => edit({ kind: "pixel", item: pixel })}>Edit</button>
              </span>
            </td>
          </tr>
        {/snippet}
      </Table>
    </section>

    <Modal bind:open={formOpen} title={editing ? formTitle(editing) : ""} description={editing?.kind === "pairing" ? `Connect the Syncthing app on ${editing.item.name} to Darkroom. Nothing is shared until you approve the Pixel here.` : undefined}>
      {#if editing?.kind === "person"}
        {@const person = editing.item}
        <PersonForm {person} onsaved={saved} oncancel={() => (formOpen = false)} ondelete={person ? () => removePerson(person) : undefined} />
      {:else if editing?.kind === "account"}
        {@const account = editing.item}
        <AccountForm {account} people={data.people} onsaved={saved} oncancel={() => (formOpen = false)} ondelete={account ? () => removeAccount(account) : undefined} />
      {:else if editing?.kind === "pixel"}
        {@const pixel = editing.item}
        <PixelForm {pixel} accounts={data.accounts} people={data.people} onsaved={saved} oncancel={() => (formOpen = false)} ondelete={pixel ? () => removePixel(pixel) : undefined}
          pairing={data.pairing} onadded={added} onpair={pixel ? () => pair(pixel) : undefined}
          onunpair={pixel?.syncthing_device_id ? () => unpairPixel(pixel) : undefined} />
      {:else if editing?.kind === "pairing"}
        <PixelPairing pixel={editing.item} onpaired={paired} onlater={() => (formOpen = false)} />
      {:else if editing?.kind === "paired"}
        <PixelPaired name={editing.item.name} ondone={() => (formOpen = false)} />
      {/if}
    </Modal>
  {/if}

  {#if asking}
    <Confirm bind:open={confirmOpen} title={asking.title} message={asking.message} detail={asking.detail} confirm={asking.confirm} run={asking.run} done={removed} />
  {/if}
</div>
