<script lang="ts">
  import { onMount } from "svelte";
  import { apiCall } from "$lib/contract";
  import { describeEvent } from "$lib/events";
  import { ago, checkedIn, count, names, nowSeconds, onDay, when } from "$lib/format";
  import { Loader } from "$lib/loader.svelte";
  import { followLink } from "$lib/nav.svelte";
  import { alertChip, NOT_CHECKED, needsAttention, needsSentence, sessionChip } from "$lib/view";
  import Chip from "../components/Chip.svelte";
  import LoadFailed from "../components/LoadFailed.svelte";

  const loader = new Loader(async () => {
    const [status, events] = await Promise.all([
      apiCall<"GET /api/status">("/api/status", { failed: "Couldn't load the status" }),
      apiCall<"GET /api/events">("/api/events?limit=10", { failed: "Couldn't load the events" }),
    ]);
    return { status, events: events.events, now: nowSeconds() };
  });

  onMount(() => void loader.load());
</script>

<div class="flex flex-col gap-6">
  <div class="flex flex-col gap-1">
    <h1 class="text-xl font-bold tracking-tight">Status</h1>
    {#if loader.current.kind === "ready"}
      <p class="text-ink-muted">
        {needsSentence(loader.current.data.status.alerts.length)}
        {loader.current.data.status.worker_last_seen === null
          ? "The browser worker has not reported yet."
          : `The browser worker was last seen ${ago(loader.current.data.status.worker_last_seen, loader.current.data.now)}.`}
      </p>
    {/if}
  </div>

  {#if loader.current.kind === "loading"}
    <p role="status" class="text-ink-muted">Loading...</p>
  {:else if loader.current.kind === "error"}
    <LoadFailed message={loader.current.message} retry={() => loader.load()} />
  {:else}
    {@const data = loader.current.data}
    {#if data.status.alerts.length > 0}
      <section class="flex flex-col gap-2.5" aria-label="Open alerts">
        <h2 class="label">Needs you, {data.status.alerts.length}</h2>
        {#each data.status.alerts as alert (alert.id)}
          {@const chip = alertChip(alert.kind)}
          <div class="flex flex-col gap-2.5 rounded-lg border border-accent bg-surface p-4 md:flex-row md:items-center md:justify-between md:gap-4 md:px-5">
            <div class="flex flex-col gap-1">
              <div><Chip tone={chip.tone} label={chip.label} /></div>
              <p class="font-bold">{alert.message}</p>
              <p class="text-ink-muted">Opened {ago(alert.opened, data.now)}.</p>
            </div>
            {#if alert.kind === "sign_in_needed" || alert.kind === "wrong_account"}
              <a class="btn btn-primary" href="/browser" onclick={(e) => followLink(e, "/browser")}>Open remote browser</a>
            {/if}
          </div>
        {/each}
      </section>
    {/if}

    <section class="flex flex-col gap-2.5" aria-label="People">
      <h2 class="label">People</h2>
      {#if data.status.people.length === 0}
        <div class="card">
          <p>No one has been added yet.</p>
          <a class="btn btn-primary self-start" href="/people" onclick={(e) => followLink(e, "/people")}>Add the first person</a>
        </div>
      {:else}
        <div class="grid gap-4 md:grid-cols-2">
          {#each data.status.people as person (person.id)}
            <article class="card" aria-label={person.name}>
              <div class="flex items-center justify-between gap-3">
                <h3 class="text-md font-semibold">{person.name}</h3>
                {#if needsAttention(person)}<Chip tone="warn" label="Needs attention" />{:else}<Chip tone="ok" label="Healthy" />{/if}
              </div>
              <dl class="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-x-3 gap-y-2 md:grid-cols-[8.5rem_minmax(0,1fr)]">
                <dt class="text-ink-muted">Originals</dt>
                <dd>{count(person.indexed, "photo")}, {person.with_gps} with GPS, {person.unknown_time} without a time</dd>
                <dt class="text-ink-muted">Google account</dt>
                <dd class="flex flex-col gap-1">
                  {#each person.accounts as account (account.id)}
                    {@const chip = sessionChip(account.session_state)}
                    <span class="flex flex-wrap items-center gap-2">{account.email} <Chip tone={chip.tone} label={chip.label} />
                      {#if account.schedule_end !== null}<span class="text-ink-muted">Schedule ends {onDay(account.schedule_end)}</span>{/if}
                    </span>
                  {:else}
                    <span class="text-ink-muted">None</span>
                  {/each}
                </dd>
                <dt class="text-ink-muted">Pixel</dt>
                <dd>{names(person.pixels.map((p) => p.name), "None")}</dd>
                <dt class="text-ink-muted">iPhone</dt>
                <dd class="flex flex-col gap-1">
                  {#each person.devices as device (device.id)}
                    <span>{device.name}: {checkedIn(device.last_checkin, data.now)}, {device.pending_uploads} pending</span>
                  {:else}
                    <span class="text-ink-muted">None</span>
                  {/each}
                </dd>
                <dt class="text-ink-muted">Last import</dt>
                <dd>{person.last_import === null ? "None yet" : when(person.last_import, data.now)}</dd>
                <dt class="text-ink-muted">Unmatched</dt>
                <dd class:text-ink-muted={person.unmatched === null}>{person.unmatched === null ? NOT_CHECKED : count(person.unmatched, "original")}</dd>
              </dl>
              <a class="font-semibold underline" href="/people" onclick={(e) => followLink(e, "/people")}>Open {person.name}</a>
            </article>
          {/each}
        </div>
      {/if}
    </section>

    <section class="card" aria-label="Recent events">
      <h2 class="text-md font-semibold">Recent events</h2>
      {#if data.events.length === 0}
        <p class="text-ink-muted">Nothing has happened yet.</p>
      {:else}
        <ul>
          {#each data.events as event (event.id)}
            <li class="flex flex-col gap-0.5 border-t border-raised py-2.5 first:border-t-0 md:grid md:grid-cols-[8rem_minmax(0,1fr)] md:gap-3">
              <span class="text-ink-muted">{when(event.time, data.now)}</span>
              <span>{describeEvent(event)}</span>
            </li>
          {/each}
        </ul>
      {/if}
    </section>
  {/if}
</div>
