<script lang="ts">
  import { onMount } from "svelte";
  import { act, actGet } from "$lib/act";
  import { apiCall } from "$lib/contract";
  import { nowSeconds, onDay, onDayAt, untilDay, when } from "$lib/format";
  import { Loader } from "$lib/loader.svelte";
  import { followLink } from "$lib/nav.svelte";
  import { accountExports, checkNowSentence, currentExport, earlierExports, exportChip, failedCaptures, JOB_NAMES, jobSentence, lastExportSentence, latestJob, maybeCreated, openJob, partChip, partsLabel, reviewCount, sessionChip } from "$lib/view";
  import Chip from "../components/Chip.svelte";
  import LoadFailed from "../components/LoadFailed.svelte";
  import Table from "../components/Table.svelte";

  const REFRESH_MS = 10_000;
  const DOWNLOAD_LIMIT = 5;

  const loader = new Loader(async () => {
    const [accounts, jobs, exports, imports] = await Promise.all([
      apiCall<"GET /api/accounts">("/api/accounts", { failed: "Couldn't load the Google accounts" }),
      apiCall<"GET /api/browser/jobs">("/api/browser/jobs", { failed: "Couldn't load the browser jobs" }),
      apiCall<"GET /api/exports">("/api/exports", { failed: "Couldn't load the exports" }),
      apiCall<"GET /api/imports">("/api/imports", { failed: "Couldn't load the imports" }),
    ]);
    return { accounts: accounts.accounts, jobs: jobs.jobs, exports: exports.exports, schedules: exports.schedules, imports: imports.imports, now: nowSeconds() };
  });

  let busy = $state(0);
  let error = $state("");
  let notice = $state("");

  onMount(() => {
    void loader.load();
    const timer = setInterval(() => void loader.load(true), REFRESH_MS);
    return () => clearInterval(timer);
  });

  async function start(request: () => Promise<unknown>): Promise<void> {
    error = "";
    notice = "";
    const started = await act(request, { busy: (on) => (busy += on ? 1 : -1), onError: (message) => (error = message) });
    if (started) await loader.load(true);
  }

  async function checkNow(): Promise<void> {
    error = "";
    notice = "";
    const reply = await actGet(() => apiCall<"POST /api/exports/check-now">("/api/exports/check-now", { method: "POST" }), { busy: (on) => (busy += on ? 1 : -1), onError: (message) => (error = message) });
    if (reply !== null) {
      notice = checkNowSentence(reply.jobs.length, reply.skipped);
      await loader.load(true);
    }
  }

  const connect = (id: number) => start(() => apiCall<"POST /api/accounts/{id}/connect">(`/api/accounts/${id}/connect`, { method: "POST" }));
  const checked = (id: number) => start(() => apiCall<"POST /api/accounts/{id}/schedule/checked">(`/api/accounts/${id}/schedule/checked`, { method: "POST" }));
  const schedule = (id: number) => start(() => apiCall<"POST /api/accounts/{id}/schedule">(`/api/accounts/${id}/schedule`, { method: "POST" }));
</script>

<div class="flex flex-col gap-6">
  <div class="flex flex-wrap items-end justify-between gap-4">
    <div class="flex flex-col gap-1">
      <h1 class="text-xl font-bold tracking-tight">Exports</h1>
      <p class="text-ink-muted">Google Takeout for each Google account: its browser session, its export schedule and its exports, downloaded and imported automatically.</p>
    </div>
    <button type="button" class="btn btn-primary w-full md:w-auto" disabled={busy > 0 || loader.current.kind !== "ready"} onclick={checkNow}>Check exports now</button>
  </div>

  {#if error}
    <p role="alert" class="rounded-md border border-danger bg-danger-bg px-3 py-2.5 text-danger">{error}</p>
  {/if}
  {#if notice}
    <p role="status" class="rounded-md border border-line bg-surface px-3 py-2.5">{notice}</p>
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
      {@const maybe = maybeCreated(data.jobs, account.id)}
      {@const failed = failedCaptures(data.jobs, account.id)}
      {@const checking = openJob(data.jobs, account.id, "status")}
      {@const notes = [jobSentence(connecting ?? latestJob(data.jobs, account.id, "connect"), "Connect"), jobSentence(scheduling ?? latestJob(data.jobs, account.id, "schedule"), "Create schedule"), jobSentence(checking ?? latestJob(data.jobs, account.id, "status"), "Check exports")].filter((note) => note !== null)}
      {@const mine = accountExports(data.exports, account.id)}
      {@const current = currentExport(mine)}
      {@const earlier = earlierExports(mine, current)}
      {@const times = data.schedules.find((each) => each.account_id === account.id)}
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
            {#if account.schedule_end !== null}
              <dd class="text-ink-muted">{untilDay(account.schedule_end, data.now)}</dd>
            {/if}
          </div>
          <div class="flex flex-col gap-0.5">
            <dt class="label">Last export</dt>
            <dd class="text-lg font-bold">{times?.last_export_at == null ? "None yet" : onDayAt(times.last_export_at)}</dd>
            <dd class="text-ink-muted">{lastExportSentence(mine[0])}</dd>
          </div>
          <div class="flex flex-col gap-0.5">
            <dt class="label">Next export</dt>
            <dd class="text-lg font-bold">{times?.next_export_at == null ? "None planned" : onDay(times.next_export_at)}</dd>
            <dd class="text-ink-muted">Darkroom checks daily</dd>
          </div>
        </dl>
        {#each notes as note (note)}
          <p class="text-ink-muted">{note}</p>
        {/each}
        {#each failed as job (job.id)}
          <p class="text-ink-muted">
            Capture of the failed {JOB_NAMES[job.kind] ?? "job"}:
            {#if job.capture_files.includes("screenshot.png")}
              <a class="font-semibold underline" href="/api/browser/failures/{job.id}/screenshot.png" target="_blank" rel="noopener">screenshot</a>{job.capture_files.length > 1 ? "," : "."}
            {/if}
            {#if job.capture_files.includes("page.html")}
              <a class="font-semibold underline" href="/api/browser/failures/{job.id}/page.html" target="_blank" rel="noopener">page snapshot</a>.
            {/if}
          </p>
        {/each}
        {#if maybe}
          <div class="flex flex-col gap-2 rounded-md border border-warn bg-warn-bg p-3 text-warn" role="status">
            <p>Takeout may already have this schedule. Open Takeout's export list and look before creating another.</p>
            <button type="button" class="btn self-start" disabled={busy > 0} onclick={() => checked(account.id)}>I checked Takeout, there is no schedule</button>
          </div>
        {/if}
        <div class="flex flex-col gap-2 md:flex-row md:flex-wrap">
          {#if connecting?.status === "waiting_for_user" || scheduling?.status === "waiting_for_user" || checking?.status === "waiting_for_user"}
            <a class="btn btn-primary" href="/browser" onclick={(e) => followLink(e, "/browser")}>Open remote browser</a>
          {/if}
          <button type="button" class="btn" disabled={busy > 0 || connecting !== undefined} onclick={() => connect(account.id)}>Connect</button>
          <button type="button" class="btn" disabled={busy > 0 || scheduling !== undefined || account.schedule_created !== null || maybe !== undefined} onclick={() => schedule(account.id)}>Create schedule</button>
        </div>
        {#if account.schedule_created !== null}
          <p class="text-ink-muted">The schedule exports Google Photos every 2 months for 1 year.</p>
        {:else}
          <p class="text-ink-muted">Create schedule exports only Google Photos as .zip parts of 50 GB, with a download link by email, every 2 months for 1 year.</p>
        {/if}
      </section>
      {#if current}
        {@const chip = exportChip(current)}
        <section class="card" aria-label="Current export parts for {account.email}">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <h2 class="text-md font-semibold">Export started {onDay(current.created_at ?? 0)}{current.part_count === null ? "" : `, ${partsLabel(current.part_count)}`}</h2>
            <Chip tone={chip.tone} label={chip.label} />
          </div>
          {#if current.parts.length === 0}
            <p class="text-ink-muted">Google is still preparing this export. Darkroom checks again daily and starts downloading when it is ready.</p>
          {:else}
            <Table label="Parts of the export started {onDay(current.created_at ?? 0)} for {account.email}" columns={["Part", "File", "Progress", "Downloads used", "State"]} rows={current.parts} empty="">
              {#snippet row(part)}
                {@const state = partChip(part, current.status)}
                <tr>
                  <td data-label="Part"><b>{part.number}</b></td>
                  <td data-label="File" class="font-mono text-sm break-all">{part.filename ?? "Not downloaded yet"}</td>
                  <td data-label="Progress" class="md:w-75">
                    <div class="flex w-full items-center gap-2.5">
                      <div class="h-2.5 w-full overflow-hidden rounded-full bg-raised" role="progressbar" aria-label="Part {part.number} download" aria-valuenow={part.progress} aria-valuemin="0" aria-valuemax="100">
                        <b class="block h-full bg-ink" style="width: {part.progress}%"></b>
                      </div>
                      <span class="w-11 shrink-0 text-right">{part.progress}%</span>
                    </div>
                  </td>
                  <td data-label="Downloads used">{part.downloads_used} of {DOWNLOAD_LIMIT}</td>
                  <td data-label="State"><Chip tone={state.tone} label={state.label} /></td>
                </tr>
              {/snippet}
            </Table>
            <p class="text-sm text-ink-muted">Google allows each part to be downloaded {DOWNLOAD_LIMIT} times. Darkroom downloads each part once and keeps the file until it is imported.</p>
          {/if}
        </section>
      {/if}
      {#if earlier.length > 0}
        <section class="card" aria-label="Earlier exports for {account.email}">
          <h2 class="text-md font-semibold">Earlier exports</h2>
          <Table label="Earlier exports for {account.email}" columns={["Exported", "Parts", "Result"]} rows={earlier} empty="">
            {#snippet row(item)}
              {@const chip = exportChip(item)}
              {@const review = reviewCount(data.imports, item.id)}
              <tr>
                <td data-label="Exported">{onDay(item.created_at ?? 0)}</td>
                <td data-label="Parts">{item.part_count ?? "None"}</td>
                <td data-label="Result">
                  <span class="inline-flex flex-wrap items-center justify-end gap-2 md:justify-start">
                    <Chip tone={chip.tone} label={chip.label} />
                    {#if item.status === "expired"}
                      <span>Never downloaded</span>
                    {:else if review > 0}
                      <span>{review} sent to review</span>
                    {/if}
                  </span>
                </td>
              </tr>
            {/snippet}
          </Table>
        </section>
      {/if}
    {/each}
  {/if}
</div>
