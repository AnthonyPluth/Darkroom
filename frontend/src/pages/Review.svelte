<script lang="ts">
  import { onMount } from "svelte";
  import type { DecisionView, ReviewSummary } from "$lib/api-types";
  import { act } from "$lib/act";
  import { apiCall } from "$lib/contract";
  import { nowSeconds, when } from "$lib/format";
  import { Loader } from "$lib/loader.svelte";
  import { camera, canUndo, compared, decisionChip, frame, pairLabel, personName, savedSentence, size, TIERS, tierLabel, wallClock } from "$lib/review";
  import Chip from "../components/Chip.svelte";
  import Confirm from "../components/Confirm.svelte";
  import LoadFailed from "../components/LoadFailed.svelte";
  import Table from "../components/Table.svelte";
  import FormError from "../forms/FormError.svelte";

  type Saved = { id: number; message: string };

  let tier = $state("");
  let decisionPage = $state(0);
  let decisionRows = $state<DecisionView[]>([]);
  let decisionTotal = $state(0);
  let saved = $state<Saved | null>(null);
  let busy = $state(false);
  let error = $state("");
  let reassignTo = $state("");
  let reversing = $state<DecisionView | null>(null);
  let confirmOpen = $state(false);

  const loader = new Loader(async () => {
    const [pending, resolved, people] = await Promise.all([
      apiCall<"GET /api/review">("/api/review?status=pending", { failed: "Couldn't load the review queue" }),
      Promise.all([
        apiCall<"GET /api/review">("/api/review?status=kept_both", { failed: "Couldn't load the past decisions" }),
        apiCall<"GET /api/review">("/api/review?status=skipped", { failed: "Couldn't load the past decisions" }),
      ]),
      apiCall<"GET /api/people">("/api/people", { failed: "Couldn't load the people" }),
    ]);
    const first = pending.reviews[0];
    const detail = first ? await apiCall<"GET /api/review/{id}">(`/api/review/${first.id}`, { failed: "Couldn't load the pair" }) : null;
    const past = resolved.flatMap((list) => list.reviews).sort((a, b) => (b.resolved_at ?? 0) - (a.resolved_at ?? 0));
    return { pending: pending.reviews, detail, past, people: people.people, now: nowSeconds() };
  });

  const decisionsLoader = new Loader(async () => {
    const query = tier ? `&tier=${tier}` : "";
    return apiCall<"GET /api/decisions">(`/api/decisions?page=${decisionPage}${query}`, { failed: "Couldn't load the decisions" });
  });

  async function loadDecisions(more = false): Promise<void> {
    decisionPage = more ? decisionPage + 1 : 0;
    await decisionsLoader.load(more);
    if (decisionsLoader.current.kind === "ready") {
      const data = decisionsLoader.current.data;
      decisionRows = more ? [...decisionRows, ...data.decisions] : data.decisions;
      decisionTotal = data.total;
    }
  }

  onMount(() => {
    void loader.load();
    void loadDecisions();
  });

  async function refresh(): Promise<void> {
    await Promise.all([loader.load(true), loadDecisions()]);
  }

  async function resolve(id: number, action: "keep_both" | "skip", name: string | null): Promise<void> {
    error = "";
    const ok = await act(() => apiCall<"POST /api/review/{id}/resolve">(`/api/review/${id}/resolve`, { method: "POST", body: { action } }), {
      busy: (on) => (busy = on), onError: (m) => (error = m),
    });
    if (ok) {
      saved = { id, message: savedSentence(action, name) };
      await refresh();
    }
  }

  async function keepFor(id: number, name: string | null, personId: number): Promise<void> {
    error = "";
    let kept: ReviewSummary | null = null;
    const ok = await act(async () => {
      kept = (await apiCall<"POST /api/review/{id}/resolve">(`/api/review/${id}/resolve`, { method: "POST", body: { action: "keep_both" } })).review;
      if (kept.kept_item_id !== null && kept.kept_person_id !== personId) {
        await apiCall<"POST /api/media/{id}/reassign">(`/api/media/${kept.kept_item_id}/reassign`, { method: "POST", body: { person_id: personId } });
      }
    }, { busy: (on) => (busy = on), onError: (m) => (error = m) });
    if (ok || kept !== null) saved = { id, message: savedSentence("keep_both", name) };
    await refresh();
  }

  async function undo(id: number): Promise<void> {
    error = "";
    const ok = await act(() => apiCall<"POST /api/review/{id}/undo">(`/api/review/${id}/undo`, { method: "POST" }), {
      busy: (on) => (busy = on), onError: (m) => (error = m),
    });
    if (ok) {
      if (saved?.id === id) saved = null;
      await refresh();
    }
  }

  function askReverse(decision: DecisionView): void {
    reversing = decision;
    confirmOpen = true;
  }

  function reversed(): void {
    confirmOpen = false;
    void refresh();
  }

  function decisionState(decision: DecisionView): string {
    if (decision.reversed_at !== null) return "Reversed";
    if (decision.review_status === "pending") return "In the review queue";
    return "";
  }
</script>

<div class="flex flex-col gap-6">
  <div class="flex flex-col gap-1">
    <h1 class="text-xl font-bold tracking-tight">Review queue</h1>
    {#if loader.current.kind === "ready"}
      {@const count = loader.current.data.pending.length}
      <p class="text-ink-muted">
        {count === 0
          ? "Nothing to review. Pairs that look like the same photo saved twice show up here."
          : `${count === 1 ? "1 pair looks" : `${count} pairs look`} like the same photo saved twice. Nothing is removed until you decide.`}
      </p>
    {/if}
  </div>

  {#if saved}
    {@const savedId = saved.id}
    <div role="status" class="flex flex-col gap-2 rounded-md border border-ok bg-ok-bg p-3 md:flex-row md:items-center md:justify-between md:gap-4 md:py-2 md:pr-2 md:pl-4">
      <span class="flex items-center gap-2 font-semibold"><span aria-hidden="true" class="block size-2 flex-none rounded-full bg-ok"></span>{saved.message}</span>
      <button type="button" class="btn" disabled={busy} onclick={() => undo(savedId)}>Undo</button>
    </div>
  {/if}

  <FormError message={error} />

  {#if loader.current.kind === "loading"}
    <p role="status" class="text-ink-muted">Loading...</p>
  {:else if loader.current.kind === "error"}
    <LoadFailed message={loader.current.message} retry={() => loader.load()} />
  {:else}
    {@const data = loader.current.data}
    {#if data.detail}
      {@const pair = data.detail}
      {@const pairId = pair.review.id}
      {@const choices = pair.served_people}
      <section class="card md:p-5" aria-label="Current pair">
        <div class="flex items-center justify-between gap-2">
          <h2 class="text-md font-semibold">Pair 1 of {data.pending.length}</h2>
          <Chip tone="warn" label="Likely re-encode" />
        </div>
        <div class="grid grid-cols-2 gap-2.5 md:gap-6">
          {#each [{ key: "library", label: "In library", alt: "The photo already in the library" }, { key: "export", label: "From export", alt: "The photo from the export" }] as side (side.key)}
            {@const fields = side.key === "library" ? pair.library : pair.export}
            {@const other = side.key === "library" ? pair.export : pair.library}
            <div class="flex min-w-0 flex-col gap-3">
              <div class="relative flex h-38 items-end overflow-hidden rounded-md border border-line bg-raised p-2 md:h-75 md:p-3">
                {#if fields && (side.key === "library" || pair.export.held)}
                  <img class="absolute inset-0 size-full object-contain" src="/api/review/{pairId}/thumb/{side.key}" alt={side.alt} />
                {/if}
                <span class="relative rounded-sm bg-ink px-2 py-0.5 text-xs font-semibold text-surface">{side.label}</span>
              </div>
              {#if fields}
                <dl class="grid grid-cols-1 gap-0 md:grid-cols-[110px_minmax(0,1fr)] md:gap-x-3 md:gap-y-1.5">
                  <dt class="mt-1.5 text-xs text-ink-muted md:mt-0 md:text-base">File</dt>
                  <dd class="font-mono text-sm break-all">{fields.name ?? "Unknown"}</dd>
                  <dt class="mt-1.5 text-xs text-ink-muted md:mt-0 md:text-base">Size</dt>
                  <dd class:text-ink-muted={fields.bytes === null}>{fields.bytes === null ? "Not checked yet" : side.key === "export" ? compared(fields.bytes, other?.bytes ?? null) : size(fields.bytes)}</dd>
                  <dt class="mt-1.5 text-xs text-ink-muted md:mt-0 md:text-base">Capture time</dt>
                  <dd class:text-ink-muted={wallClock(fields.captured_local) === null}>{wallClock(fields.captured_local) ?? "Unknown"}</dd>
                  <dt class="mt-1.5 text-xs text-ink-muted md:mt-0 md:text-base">Camera</dt>
                  <dd>{camera(fields)}</dd>
                  <dt class="mt-1.5 text-xs text-ink-muted md:mt-0 md:text-base">{fields.is_video ? "Length" : "Dimensions"}</dt>
                  <dd>{frame(fields)}</dd>
                  {#if side.key === "library" && pair.library}
                    <dt class="mt-1.5 hidden text-xs text-ink-muted md:mt-0 md:block md:text-base">Person</dt>
                    <dd class="hidden md:block">{personName(data.people, pair.library.person_id)}</dd>
                  {:else}
                    <dt class="mt-1.5 hidden text-xs text-ink-muted md:mt-0 md:block md:text-base">Person</dt>
                    <dd class="hidden md:block">Unmatched</dd>
                  {/if}
                </dl>
              {:else}
                <p class="text-ink-muted">The library photo is no longer there.</p>
              {/if}
            </div>
          {/each}
        </div>
        <div class="flex flex-col gap-3 border-t border-raised pt-4 md:flex-row md:flex-wrap md:items-end">
          <button type="button" class="btn btn-primary" disabled={busy || !pair.export.held} onclick={() => resolve(pairId, "keep_both", pair.review.name)}>Keep both</button>
          <button type="button" class="btn" disabled={busy} onclick={() => resolve(pairId, "skip", pair.review.name)}>Skip</button>
          {#if choices.length > 1}
            <div class="hidden flex-1 md:block"></div>
            <div class="flex flex-col gap-1 border-t border-raised pt-3 md:border-0 md:pt-0">
              <label class="label" for="reassign-to">Reassign to</label>
              <select id="reassign-to" class="input md:w-auto" bind:value={reassignTo}>
                {#each choices as person (person.id)}
                  <option value={String(person.id)}>{person.name}</option>
                {/each}
              </select>
            </div>
            <button type="button" class="btn" disabled={busy || !pair.export.held}
              onclick={() => keepFor(pairId, pair.review.name, Number(reassignTo || String(pair.default_person_id ?? choices[0].id)))}>Reassign</button>
          {/if}
        </div>
        <p class="text-sm text-ink-muted">
          Keep both stores the two files. Skip keeps only the library photo; the export's copy is held for 14 days, so you can undo.
          {#if choices.length > 1}Reassign keeps both and gives the export file to the person you choose.{/if}
        </p>
        {#if !pair.export.held}<p class="text-sm text-ink-muted">The export's held copy is gone, so this pair can only be skipped.</p>{/if}
      </section>
    {/if}

    <section class="card md:p-5" aria-label="Past decisions">
      <h2 class="text-md font-semibold">Past decisions</h2>
      <Table label="Past decisions" columns={["When", "Pair", "Decision", ""]} rows={data.past} empty="No decisions yet.">
        {#snippet row(review)}
          {@const chip = decisionChip(review, data.people)}
          <tr>
            <td data-label="When">{review.resolved_at === null ? "" : when(review.resolved_at, data.now)}</td>
            <td data-label="Pair" class="font-mono text-sm break-all">{pairLabel(review)}</td>
            <td data-label="Decision"><Chip tone={chip.tone} label={chip.label} /></td>
            <td class="md:text-right">
              {#if canUndo(review, data.now)}
                <button type="button" class="btn w-full md:w-auto" disabled={busy} aria-label="Undo {pairLabel(review)}" onclick={() => undo(review.id)}>Undo</button>
              {:else}
                <span class="text-ink-muted">Too late to undo</span>
              {/if}
            </td>
          </tr>
        {/snippet}
      </Table>
    </section>

    <section class="card md:p-5" aria-label="Dedupe decisions">
      <div class="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div class="flex flex-col gap-0.5">
          <h2 class="text-md font-semibold">Dedupe decisions</h2>
          <span class="text-ink-muted">Every match an import made. Reversing one puts the photo back to unmatched.</span>
        </div>
        <div class="flex flex-col gap-1">
          <label class="label" for="tier-filter">Tier</label>
          <select id="tier-filter" class="input md:w-auto" bind:value={tier} onchange={() => loadDecisions()}>
            <option value="">All tiers</option>
            {#each TIERS as option (option.tier)}
              <option value={String(option.tier)}>{option.label}</option>
            {/each}
          </select>
        </div>
      </div>
      {#if decisionsLoader.current.kind === "error"}
        <LoadFailed message={decisionsLoader.current.message} retry={() => loadDecisions()} />
      {:else}
        <Table label="Dedupe decisions" columns={["When", "File", "Tier", "Person", ""]} rows={decisionRows} empty="No decisions yet.">
          {#snippet row(decision)}
            {@const state = decisionState(decision)}
            <tr>
              <td data-label="When">{when(decision.decided_at, data.now)}</td>
              <td data-label="File" class="font-mono text-sm break-all">{decision.name ?? "Unknown file"}</td>
              <td data-label="Tier">{tierLabel(decision.tier)}</td>
              <td data-label="Person">{personName(data.people, decision.person_id)}</td>
              <td class="md:text-right">
                {#if state}
                  <span class="text-ink-muted">{state}</span>
                {:else}
                  <button type="button" class="btn w-full md:w-auto" aria-label="Reverse {decision.name ?? 'decision'}" onclick={() => askReverse(decision)}>Reverse</button>
                {/if}
              </td>
            </tr>
          {/snippet}
        </Table>
        {#if decisionRows.length < decisionTotal}
          <div class="flex items-center justify-between gap-3">
            <span class="text-ink-muted">Showing {decisionRows.length} of {decisionTotal}</span>
            <button type="button" class="btn" onclick={() => loadDecisions(true)}>Show more</button>
          </div>
        {/if}
      {/if}
    </section>
  {/if}

  {#if reversing}
    {@const target = reversing}
    <Confirm bind:open={confirmOpen} title="Reverse this decision?"
      message={target.tier <= 2
        ? `${target.name ?? "This photo"} will count as not yet in Google, so its iPhone original stops being safe to delete until another match confirms it. Files stay where they are.`
        : `The record for ${target.name ?? "this photo"} is marked reversed. Files stay where they are.`}
      confirm="Reverse decision"
      run={() => apiCall<"POST /api/decisions/{id}/reverse">(`/api/decisions/${target.id}/reverse`, { method: "POST" })}
      done={reversed} />
  {/if}
</div>
