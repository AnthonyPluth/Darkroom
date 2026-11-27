<script lang="ts">
  import { onMount } from "svelte";
  import type { Holds, PersonView } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import { Loader } from "$lib/loader.svelte";
  import { personName } from "$lib/review";
  import LoadFailed from "./LoadFailed.svelte";
  import HeldPhoto from "./HeldPhoto.svelte";
  import FormError from "../forms/FormError.svelte";

  let { people, onCount }: { people: readonly Pick<PersonView, "id" | "name">[]; onCount: (count: number | null) => void } = $props();
  let after: number | null = null;
  let more = $state(false);
  let conflict = $state("");
  const loader: Loader<Holds & { hasMore: boolean }> = new Loader(async () => {
    const cursor = after;
    const prior = cursor !== null && loader.current.kind === "ready" ? loader.current.data.holds : [];
    const page = await apiCall<"GET /api/holds">(cursor === null ? "/api/holds" : `/api/holds?after=${cursor}`);
    const seen = new Set(prior.map((row) => row.id));
    return { ...page, holds: [...prior, ...page.holds.filter((row) => !seen.has(row.id))], hasMore: page.holds.length > 0 };
  });

  $effect(() => onCount(loader.current.kind === "ready" ? loader.current.data.total : null));
  onMount(() => void refresh());

  async function refresh(message = ""): Promise<void> {
    conflict = message;
    after = null;
    await loader.load(loader.current.kind === "ready");
  }

  async function loadMore(): Promise<void> {
    if (more || loader.current.kind !== "ready") return;
    after = loader.current.data.holds.at(-1)?.id ?? null;
    if (after === null) return;
    more = true;
    try {
      await loader.load(true);
    } finally {
      more = false;
    }
  }
</script>

<section class="card scroll-mt-4 md:p-5 max-md:[&_.btn]:min-h-[44px] max-md:[&_.input]:min-h-[44px]" id="held-photos" aria-labelledby="held-photos-heading">
  <h2 id="held-photos-heading" class="text-md font-semibold">Held photos</h2>
  <FormError message={conflict} />
  {#if loader.current.kind === "loading"}
    <p role="status" class="text-ink-muted">Loading held photos…</p>
  {:else if loader.current.kind === "error"}
    <LoadFailed message="Couldn’t load held photos." retry={() => refresh()} />
  {:else}
    {@const data = loader.current.data}
    {#if data.holds.length === 0}
      <p class="text-ink-muted">No held photos.</p>
    {:else}
      <div class="flex flex-col">
        {#each data.holds as row (row.id)}
          <HeldPhoto {row} person={personName(people, row.person_id)} {refresh} />
        {/each}
      </div>
      {#if data.hasMore && data.holds.length < data.total}
        <button type="button" class="btn self-start" disabled={more} onclick={loadMore}>Load more held photos</button>
      {/if}
    {/if}
  {/if}
</section>
