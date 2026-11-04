<script lang="ts">
  import { onMount } from "svelte";
  import type { ImportView, MediaView } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import { nowSeconds, onDay, when } from "$lib/format";
  import { Loader } from "$lib/loader.svelte";
  import { followLink } from "$lib/nav.svelte";
  import { camera, counted, partsSentence, personName, RANGES, rangeStart, type RangeKey, runChip, skipped, unmatchedNames, unmatchedTotal, wallClock } from "$lib/review";
  import { NOT_CHECKED } from "$lib/view";
  import Chip from "../components/Chip.svelte";
  import LoadFailed from "../components/LoadFailed.svelte";

  const NOT_COUNTED = "Not counted yet";

  let person = $state<number | null>(null);
  let range = $state<RangeKey>("year");
  let page = $state(0);
  let missing = $state<MediaView[]>([]);
  let missingTotal = $state(0);

  const loader = new Loader(async () => {
    const [imports, people] = await Promise.all([
      apiCall<"GET /api/imports">("/api/imports", { failed: "Couldn't load the imports" }),
      apiCall<"GET /api/people">("/api/people", { failed: "Couldn't load the people" }),
    ]);
    return { runs: imports.imports, people: people.people, now: nowSeconds() };
  });

  const gps = new Loader(async () => {
    const start = rangeStart(range, nowSeconds());
    const query = [`gps=none`, `page=${page}`, ...(person === null ? [] : [`person=${person}`]), ...(start === null ? [] : [`from=${start}`])].join("&");
    return apiCall<"GET /api/media">(`/api/media?${query}`, { failed: "Couldn't load the photos without a location" });
  });

  async function loadMissing(more = false): Promise<void> {
    page = more ? page + 1 : 0;
    await gps.load(more);
    if (gps.current.kind === "ready") {
      missing = more ? [...missing, ...gps.current.data.media] : gps.current.data.media;
      missingTotal = gps.current.data.total;
    }
  }

  onMount(() => {
    void loader.load();
    void loadMissing();
  });

  function choose(next: number | null): void {
    person = next;
    void loadMissing();
  }

  function title(run: ImportView): string {
    return run.export_created_at === null ? `Export ${run.export_id}` : `Export of ${onDay(run.export_created_at)}`;
  }

  function subtitle(run: ImportView, now: number): string {
    const parts = partsSentence(run);
    return parts ? `${when(run.started, now)}, ${parts}` : when(run.started, now);
  }

  function taken(item: MediaView): string | null {
    return wallClock(item.captured_local, false);
  }
</script>

{#snippet number(value: number | null, ready: boolean, empty: string)}
  {#if !ready || value === null}<span class="text-ink-muted">{ready ? empty : NOT_COUNTED}</span>{:else}{value.toLocaleString("en-GB")}{/if}
{/snippet}

<div class="flex flex-col gap-6">
  <div class="flex flex-col gap-1">
    <h1 class="text-xl font-bold tracking-tight">Imports</h1>
    <p class="text-ink-muted">What each export added to the library, and what needs a look.</p>
  </div>

  {#if loader.current.kind === "loading"}
    <p role="status" class="text-ink-muted">Loading...</p>
  {:else if loader.current.kind === "error"}
    <LoadFailed message={loader.current.message} retry={() => loader.load()} />
  {:else}
    {@const data = loader.current.data}
    <section class="flex flex-col gap-2.5 md:gap-3.5 md:rounded-lg md:border md:border-line md:bg-surface md:p-5" aria-label="Import runs">
      <h2 class="label md:text-md md:font-semibold md:tracking-normal md:text-ink md:normal-case">Runs</h2>
      {#if data.runs.length === 0}
        <p class="text-ink-muted">No export has been imported yet.</p>
      {:else}
        <table class="hidden w-full border-collapse md:table" aria-label="Import runs">
          <thead>
            <tr>
              {#each ["Run", "Added", "Skipped as duplicates", "Sent to review", "Missing GPS", "Unmatched by person"] as column, index (column)}
                <th scope="col" class="label pr-3 pb-2 {index === 0 ? 'text-left' : 'text-right'}">{column}</th>
              {/each}
            </tr>
          </thead>
          <tbody>
            {#each data.runs as run (run.id)}
              {@const ready = counted(run)}
              {@const chip = runChip(run)}
              {@const unmatched = unmatchedNames(run, data.people)}
              <tr>
                <td class="border-t border-raised py-3 pr-3 align-middle">
                  <b>{title(run)}</b>
                  {#if chip}<Chip tone={chip.tone} label={chip.label} />{/if}<br />
                  <span class="text-ink-muted">{subtitle(run, data.now)}</span>
                  {#if run.error}<br /><span class="text-danger">{run.error}</span>{/if}
                </td>
                <td class="border-t border-raised py-3 pr-3 text-right tabular-nums">{@render number(run.added, ready, "")}</td>
                <td class="border-t border-raised py-3 pr-3 text-right tabular-nums">{@render number(skipped(run), ready, "")}</td>
                <td class="border-t border-raised py-3 pr-3 text-right tabular-nums">
                  {#if ready && run.review > 0}
                    <a class="font-semibold text-ink underline" href="/review" onclick={(e) => followLink(e, "/review")}>{run.review}</a>
                  {:else}{@render number(run.review, ready, "")}{/if}
                </td>
                <td class="border-t border-raised py-3 pr-3 text-right tabular-nums">{@render number(run.missing_gps, ready, "")}</td>
                <td class="border-t border-raised py-3 pr-3 text-right tabular-nums">
                  {@render number(unmatchedTotal(run), ready, NOT_CHECKED)}
                  {#if unmatched.length}<br /><span class="text-sm text-ink-muted">{unmatched.join(", ")}</span>{/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        <div class="flex flex-col gap-2.5 md:hidden">
          {#each data.runs as run (run.id)}
            {@const ready = counted(run)}
            {@const chip = runChip(run)}
            {@const unmatched = unmatchedNames(run, data.people)}
            <article class="card gap-3" aria-label={title(run)}>
              <div class="flex items-center justify-between gap-2">
                <b>{title(run)}</b>
                {#if chip}<Chip tone={chip.tone} label={chip.label} />{:else}<span class="text-ink-muted">{when(run.started, data.now)}</span>{/if}
              </div>
              {#if partsSentence(run)}<span class="text-ink-muted">{partsSentence(run)}</span>{/if}
              {#if run.error}<span class="text-danger">{run.error}</span>{/if}
              <div class="grid grid-cols-2 gap-2.5">
                {#each [["Added", run.added, ""], ["Skipped as duplicates", skipped(run), ""], ["Sent to review", run.review, ""], ["Missing GPS", run.missing_gps, ""], ["Unmatched by person", unmatchedTotal(run), NOT_CHECKED]] as const as [label, value, empty] (label)}
                  <div class="flex flex-col rounded-md bg-canvas px-2.5 py-2">
                    <b class="text-lg tabular-nums">{@render number(value, ready, empty)}</b>
                    <span class="text-xs text-ink-muted">{label}</span>
                  </div>
                {/each}
              </div>
              {#if unmatched.length}<span class="text-sm text-ink-muted">Unmatched: {unmatched.join(", ")}</span>{/if}
              {#if ready && run.review > 0}
                <a class="btn w-full" href="/review" onclick={(e) => followLink(e, "/review")}>Review the {run.review}</a>
              {/if}
            </article>
          {/each}
        </div>
      {/if}
    </section>

    <section class="card md:p-5" aria-label="Missing GPS">
      <div class="flex flex-col gap-3 md:flex-row md:flex-wrap md:items-end md:justify-between md:gap-4">
        <div class="flex flex-col gap-0.5">
          <h2 class="text-md font-semibold">Missing GPS</h2>
          <span class="text-ink-muted">{missingTotal === 1 ? "1 photo has" : `${missingTotal} photos have`} no location. Newest first.</span>
        </div>
        <div class="flex flex-col gap-3 md:flex-row md:items-end">
          <div role="group" aria-label="Person" class="grid grid-cols-3 gap-2 md:flex md:gap-1.5">
            <button type="button" class="btn aria-pressed:border-ink aria-pressed:bg-ink aria-pressed:text-surface" aria-pressed={person === null} onclick={() => choose(null)}>All</button>
            {#each data.people as option (option.id)}
              <button type="button" class="btn aria-pressed:border-ink aria-pressed:bg-ink aria-pressed:text-surface" aria-pressed={person === option.id} onclick={() => choose(option.id)}>{option.name}</button>
            {/each}
          </div>
          <div class="flex flex-col gap-1">
            <label class="label" for="taken-range">Taken</label>
            <select id="taken-range" class="input md:w-auto" bind:value={range} onchange={() => loadMissing()}>
              {#each RANGES as option (option.key)}
                <option value={option.key}>{option.label}</option>
              {/each}
            </select>
          </div>
        </div>
      </div>
      {#if gps.current.kind === "error"}
        <LoadFailed message={gps.current.message} retry={() => loadMissing()} />
      {:else if gps.current.kind === "loading" && missing.length === 0}
        <p role="status" class="text-ink-muted">Loading...</p>
      {:else if missing.length === 0}
        <p class="text-ink-muted">Every photo in this range has a location.</p>
      {:else}
        <ul aria-label="Photos without a location">
          {#each missing as item (item.id)}
            <li class="flex items-center gap-3 border-t border-raised py-2.5 first:border-t-0 md:grid md:grid-cols-[56px_minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)]">
              <span aria-hidden="true" class="block size-12 flex-none rounded-sm bg-raised md:size-11"></span>
              <div class="flex min-w-0 flex-col md:contents">
                <span class="font-mono text-sm break-all">{item.name ?? "Unknown file"}</span>
                <span class="text-ink-muted md:text-ink">{personName(data.people, item.person_id)}<span class="md:hidden">, {taken(item) ?? "unknown time"}, {camera(item)}</span></span>
                <span class="hidden md:block" class:text-ink-muted={taken(item) === null}>{taken(item) ?? "Unknown time"}</span>
                <span class="hidden md:block">{camera(item)}</span>
              </div>
            </li>
          {/each}
        </ul>
        <div class="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <span class="text-ink-muted">Showing {missing.length} of {missingTotal}</span>
          {#if missing.length < missingTotal}
            <button type="button" class="btn" onclick={() => loadMissing(true)}>Show more</button>
          {/if}
        </div>
      {/if}
    </section>
  {/if}
</div>
