<script lang="ts">
  import { onMount } from "svelte";
  import { act } from "$lib/act";
  import { apiCall } from "$lib/contract";
  import { nowSeconds, onDay, when } from "$lib/format";
  import { Loader } from "$lib/loader.svelte";
  import { followLink } from "$lib/nav.svelte";
  import { jobSentence, latestJob, openJob, sessionChip } from "$lib/view";
  import Chip from "../components/Chip.svelte";
  import LoadFailed from "../components/LoadFailed.svelte";

  const REFRESH_MS = 10_000;

  const loader = new Loader(async () => {
    const [accounts, jobs] = await Promise.all([
      apiCall<"GET /api/accounts">("/api/accounts", { failed: "Couldn't load the Google accounts" }),
      apiCall<"GET /api/browser/jobs">("/api/browser/jobs", { failed: "Couldn't load the browser jobs" }),
    ]);
    return { accounts: accounts.accounts, jobs: jobs.jobs, now: nowSeconds() };
  });

  let busy = $state(0);
  let error = $state("");

  onMount(() => {
    void loader.load();
    const timer = setInterval(() => void loader.load(true), REFRESH_MS);
    return () => clearInterval(timer);
  });

  async function start(request: () => Promise<unknown>): Promise<void> {
    error = "";
    const started = await act(request, { busy: (on) => (busy += on ? 1 : -1), onError: (message) => (error = message) });
    if (started) await loader.load(true);
  }

  const connect = (id: number) => start(() => apiCall<"POST /api/accounts/{id}/connect">(`/api/accounts/${id}/connect`, { method: "POST" }));
  const schedule = (id: number) => start(() => apiCall<"POST /api/accounts/{id}/schedule">(`/api/accounts/${id}/schedule`, { method: "POST" }));
</script>

<div class="flex flex-col gap-6">
  <div class="flex flex-col gap-1">
    <h1 class="text-xl font-bold tracking-tight">Exports</h1>
    <p class="text-ink-muted">Google Takeout for each Google account: whether its browser session works and when its export schedule ends.</p>
  </div>

  {#if error}
    <p role="alert" class="rounded-md border border-danger bg-danger-bg px-3 py-2.5 text-danger">{error}</p>
  {/if}

  {#if loader.current.kind === "loading"}
    <p role="status" class="text-ink-muted">Loading...</p>
  {:else if loader.current.kind === "error"}
    <LoadFailed message={loader.current.message} retry={() => loader.load()} />
  {:else}
    {@const data = loader.current.data}
    {#if data.accounts.length === 0}
      <div class="card">
        <p>No Google account has been added yet.</p>
        <a class="btn btn-primary self-start" href="/people" onclick={(e) => followLink(e, "/people")}>Add a Google account</a>
      </div>
    {/if}
    {#each data.accounts as account (account.id)}
      {@const chip = sessionChip(account.session_state)}
      {@const connecting = openJob(data.jobs, account.id, "connect")}
      {@const scheduling = openJob(data.jobs, account.id, "schedule")}
      {@const notes = [jobSentence(connecting ?? latestJob(data.jobs, account.id, "connect"), "Connect"), jobSentence(scheduling ?? latestJob(data.jobs, account.id, "schedule"), "Create schedule")].filter((note) => note !== null)}
      <section class="card" aria-label={account.email}>
        <div class="flex flex-wrap items-center justify-between gap-3">
          <h2 class="text-md font-semibold break-all">{account.email}</h2>
          <Chip tone={chip.tone} label={chip.label} />
        </div>
        <dl class="grid gap-4 md:grid-cols-3 md:gap-6">
          <div class="flex flex-col gap-0.5">
            <dt class="label">Session checked</dt>
            <dd class="text-lg font-bold">{account.session_checked === null ? "Not yet" : when(account.session_checked, data.now)}</dd>
          </div>
          <div class="flex flex-col gap-0.5">
            <dt class="label">Schedule created</dt>
            <dd class="text-lg font-bold">{account.schedule_created === null ? "Not created" : onDay(account.schedule_created)}</dd>
          </div>
          <div class="flex flex-col gap-0.5">
            <dt class="label">Schedule ends</dt>
            <dd class="text-lg font-bold">{account.schedule_end === null ? "No schedule" : onDay(account.schedule_end)}</dd>
          </div>
        </dl>
        {#each notes as note (note)}
          <p class="text-ink-muted">{note}</p>
        {/each}
        <div class="flex flex-col gap-2 md:flex-row md:flex-wrap">
          {#if connecting?.status === "waiting_for_user" || scheduling?.status === "waiting_for_user"}
            <a class="btn btn-primary" href="/browser" onclick={(e) => followLink(e, "/browser")}>Open remote browser</a>
          {/if}
          <button type="button" class="btn" disabled={busy > 0 || connecting !== undefined} onclick={() => connect(account.id)}>Connect</button>
          <button type="button" class="btn" disabled={busy > 0 || scheduling !== undefined || account.schedule_created !== null} onclick={() => schedule(account.id)}>Create schedule</button>
        </div>
        {#if account.schedule_created !== null}
          <p class="text-ink-muted">The schedule exports Google Photos every 2 months for 1 year.</p>
        {:else}
          <p class="text-ink-muted">Create schedule exports only Google Photos as .zip parts of 50 GB, with a download link by email, every 2 months for 1 year.</p>
        {/if}
      </section>
    {/each}
  {/if}
</div>
