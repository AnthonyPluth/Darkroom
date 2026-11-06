<script lang="ts">
  import { onMount } from "svelte";
  import type { AccountView, DeviceView, PersonView, PixelView } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import { act } from "$lib/act";
  import { ago, count, names, nowSeconds } from "$lib/format";
  import { Loader } from "$lib/loader.svelte";
  import { followLink } from "$lib/nav.svelte";
  import { size } from "$lib/review";
  import { NOT_CHECKED, sessionChip } from "$lib/view";
  import Chip from "../components/Chip.svelte";
  import Confirm from "../components/Confirm.svelte";
  import LoadFailed from "../components/LoadFailed.svelte";
  import Modal from "../components/Modal.svelte";
  import Table from "../components/Table.svelte";
  import AccountForm from "../forms/AccountForm.svelte";
  import PersonForm from "../forms/PersonForm.svelte";
  import FormError from "../forms/FormError.svelte";
  import PixelForm from "../forms/PixelForm.svelte";
  import Switch from "../components/Switch.svelte";

  type Editing =
    | { kind: "person"; item: PersonView | null }
    | { kind: "account"; item: AccountView | null }
    | { kind: "pixel"; item: PixelView | null };
  type Asking = { title: string; message: string; confirm: string; run: () => Promise<unknown> };

  const loader = new Loader(async () => {
    const [people, accounts, pixels, status] = await Promise.all([
      apiCall<"GET /api/people">("/api/people", { failed: "Couldn't load the people" }),
      apiCall<"GET /api/accounts">("/api/accounts", { failed: "Couldn't load the Google accounts" }),
      apiCall<"GET /api/pixels">("/api/pixels", { failed: "Couldn't load the Pixels" }),
      apiCall<"GET /api/status">("/api/status", { failed: "Couldn't load the photos eligible to delete" }),
    ]);
    const lists = await Promise.all(
      people.people.map((p) => apiCall<"GET /api/people/{id}/devices">(`/api/people/${p.id}/devices`, { failed: `Couldn't load the iPhones of ${p.name}` })),
    );
    const devices: DeviceView[] = lists.flatMap((l) => l.devices);
    const eligible = new Map(status.people.flatMap((p) => p.devices).map((d) => [d.id, d]));
    return { people: people.people, accounts: accounts.accounts, pixels: pixels.pixels, devices, eligible, now: nowSeconds() };
  });

  let formOpen = $state(false);
  let editing = $state<Editing | null>(null);
  let confirmOpen = $state(false);
  let asking = $state<Asking | null>(null);
  let switches = $state<Record<number, boolean>>({});
  let switchErrors = $state<Record<number, string>>({});
  let inFlight = $state<Record<number, boolean>>({});

  onMount(() => void loader.load());

  function edit(next: Editing): void {
    editing = next;
    formOpen = true;
  }

  function saved(): void {
    formOpen = false;
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

  function eligibleOf(data: { eligible: Map<number, { eligible: number | null; eligible_bytes: number | null }> }, device: DeviceView): { count: number; bytes: number } | null {
    const found = data.eligible.get(device.id);
    if (!found || found.eligible === null) return null;
    return { count: found.eligible, bytes: found.eligible_bytes ?? 0 };
  }

  const personName = (people: PersonView[], id: number): string => people.find((p) => p.id === id)?.name ?? "Unknown";

  function pixelLabel(pixel: PixelView, person: PersonView, people: PersonView[]): string {
    const others = pixel.people_ids.filter((id) => id !== person.id).map((id) => personName(people, id));
    return others.length ? `${pixel.name} (also ${others.join(", ")})` : pixel.name;
  }

  function formTitle(next: Editing): string {
    const noun = next.kind === "person" ? "person" : next.kind === "account" ? "Google account" : "Pixel";
    return next.item ? `Edit ${noun}` : `Add ${noun}`;
  }

  function deviceChip(device: DeviceView): { tone: "ok" | "warn" | "bad" | "neutral"; label: string } {
    if (device.revoked_at !== null) return { tone: "bad", label: "Revoked" };
    if (device.last_checkin === null) return { tone: "neutral", label: "Never checked in" };
    if (device.pending_uploads > 0) return { tone: "warn", label: "Uploads pending" };
    return { tone: "ok", label: "Checked in" };
  }

  function revoke(device: DeviceView, owner: string): void {
    ask({
      title: `Revoke ${device.name}?`,
      message: `${owner}'s iPhone will stop being able to sign in to Darkroom. Photos already uploaded stay where they are.`,
      confirm: "Revoke iPhone",
      run: () => apiCall<"DELETE /api/devices/{id}">(`/api/devices/${device.id}`, { method: "DELETE" }),
    });
  }

  const cleanupOn = (device: DeviceView): boolean => switches[device.id] ?? device.cleanup_enabled;

  const setCleanup = (device: DeviceView, enabled: boolean): Promise<unknown> =>
    apiCall<"PATCH /api/devices/{id}">(`/api/devices/${device.id}`, {
      method: "PATCH",
      body: enabled ? { cleanup_enabled: true, icloud_photos_off: true } : { cleanup_enabled: false },
    }).then((reply) => {
      switches[device.id] = reply.device.cleanup_enabled;
    });

  async function changeCleanup(device: DeviceView, enabled: boolean): Promise<void> {
    inFlight[device.id] = true;
    try {
      await setCleanup(device, enabled);
      await loader.load(true);
    } finally {
      delete switches[device.id];
      inFlight[device.id] = false;
    }
  }

  async function turnOffCleanup(device: DeviceView): Promise<void> {
    switchErrors[device.id] = "";
    switches[device.id] = false;
    await act(() => changeCleanup(device, false), { onError: (m) => (switchErrors[device.id] = m) });
  }

  function toggleCleanup(device: DeviceView, owner: string): void {
    if (inFlight[device.id]) return;
    if (cleanupOn(device)) {
      void turnOffCleanup(device);
      return;
    }
    switchErrors[device.id] = "";
    ask({
      title: `Turn on cleanup for ${device.name}, ${owner}?`,
      message: "Confirm that iCloud Photos is off on this iPhone. With it on, deleting a photo from the phone also removes it from iCloud and every other device.",
      confirm: "Turn on cleanup",
      run: () => changeCleanup(device, true),
    });
  }

  function removePerson(person: PersonView): void {
    ask({
      title: `Delete ${person.name}?`,
      message: "Only a person with no photos, iPhones, Google accounts or Pixels can be deleted. Their folders are never touched.",
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

<div class="flex flex-col gap-7">
  <div class="flex flex-col gap-1">
    <h1 class="text-xl font-bold tracking-tight">People</h1>
    <p class="text-ink-muted">Who the family is, and which Google accounts, Pixels and iPhones serve them.</p>
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
      <Table label="Persons" columns={["Person", "Immich user", "Google account", "Pixel", "iPhone", ""]} rows={data.people} empty="No one has been added yet.">
        {#snippet row(person)}
          <tr>
            <td data-label="Person"><b>{person.name}</b></td>
            <td data-label="Immich user">{person.immich_user_id ?? "Not linked"}<br /><span class="text-ink-muted">{person.has_immich_key ? "API key saved" : "No API key"}</span></td>
            <td data-label="Google account">{names(data.accounts.filter((a) => a.served_people.includes(person.id)).map((a) => a.email), "None")}</td>
            <td data-label="Pixel">{names(data.pixels.filter((p) => p.people_ids.includes(person.id)).map((p) => pixelLabel(p, person, data.people)), "None")}</td>
            <td data-label="iPhone">{names(data.devices.filter((d) => d.person_id === person.id && d.revoked_at === null).map((d) => d.name), "None")}</td>
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
            <td class="md:text-right"><button type="button" class="btn" aria-label="Edit {pixel.name}" onclick={() => edit({ kind: "pixel", item: pixel })}>Edit</button></td>
          </tr>
        {/snippet}
      </Table>
    </section>

    <section class="flex flex-col gap-3" aria-label="iPhones">
      <h2 class="text-md font-semibold">iPhones</h2>
      {#if data.devices.length === 0}
        <p class="text-ink-muted">No iPhone has signed in yet.</p>
      {:else}
        <div class="grid gap-4 md:grid-cols-2">
          {#each data.devices as device (device.id)}
            {@const chip = deviceChip(device)}
            {@const owner = personName(data.people, device.person_id)}
            <article class="card" aria-label="{device.name}, {owner}">
              <div class="flex items-center justify-between gap-3">
                <h3 class="text-md font-semibold">{device.name}, {owner}</h3>
                <Chip tone={chip.tone} label={chip.label} />
              </div>
              <dl class="grid grid-cols-[8.5rem_minmax(0,1fr)] gap-x-3 gap-y-2">
                <dt class="text-ink-muted">Last check-in</dt>
                <dd>{device.last_checkin === null ? "Never" : ago(device.last_checkin, data.now)}</dd>
                <dt class="text-ink-muted">Uploads pending</dt>
                <dd>{device.pending_uploads}</dd>
                <dt class="text-ink-muted">App version</dt>
                <dd>{device.app_version ?? "Unknown"}</dd>
                <dt class="text-ink-muted">Eligible to delete</dt>
                {#if eligibleOf(data, device) === null}
                  <dd class="text-ink-muted">{NOT_CHECKED}</dd>
                {:else}
                  <dd><b>{count(eligibleOf(data, device)?.count ?? 0, "photo")}</b> ({size(eligibleOf(data, device)?.bytes ?? 0)}), confirmed in Google Photos</dd>
                {/if}
              </dl>
              {#if device.revoked_at === null}
                {@const on = cleanupOn(device)}
                <div class="flex flex-col gap-0.5 border-t border-line pt-2">
                  <Switch checked={on} label={on ? "Cleanup on" : "Cleanup off"} name="{on ? 'Cleanup on' : 'Cleanup off'} for {device.name}, {owner}" disabled={inFlight[device.id] === true} onclick={() => toggleCleanup(device, owner)} />
                  <p class="text-sm text-ink-muted">
                    {#if on}The iPhone app may delete originals that Google Photos has confirmed.<span class="hidden md:inline"> Turn off to keep everything on the phone.</span>{:else}Nothing is deleted from this phone while cleanup is off.{/if}
                  </p>
                  <FormError message={switchErrors[device.id] ?? ""} />
                </div>
              {/if}
              {#if device.revoked_at === null}
                <button type="button" class="btn btn-danger self-start" aria-label="Revoke {device.name}, {owner}" onclick={() => revoke(device, owner)}>Revoke</button>
              {/if}
            </article>
          {/each}
        </div>
      {/if}
    </section>

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
  {/if}

  {#if asking}
    <Confirm bind:open={confirmOpen} title={asking.title} message={asking.message} confirm={asking.confirm} run={asking.run} done={removed} />
  {/if}
</div>
