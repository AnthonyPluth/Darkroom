<script lang="ts">
  import { Popover } from "bits-ui";
  import type { Me } from "$lib/api-types";
  import { followLink, pageFor, PAGES, router } from "$lib/nav.svelte";
  import { displayName } from "$lib/session.svelte";
  import Imports from "./pages/Imports.svelte";
  import Browser from "./pages/Browser.svelte";
  import Exports from "./pages/Exports.svelte";
  import People from "./pages/People.svelte";
  import Placeholder from "./pages/Placeholder.svelte";
  import Review from "./pages/Review.svelte";
  import Status from "./pages/Status.svelte";

  let { me }: { me: Me } = $props();
  const page = $derived(pageFor(router.path));
  const initial = $derived(displayName(me).split(/\s+/)[0] ?? "");
</script>

{#snippet signOut()}
  <form method="post" action="/auth/logout">
    <button type="submit" class="btn w-full">Sign out</button>
  </form>
{/snippet}

<div class="min-h-dvh md:flex">
  <aside class="hidden w-58 flex-none flex-col gap-6 border-r border-line bg-rail px-4 py-6 md:sticky md:top-0 md:flex md:h-dvh">
    <a href="/" class="flex items-center gap-2.5 px-2 text-lg font-bold" onclick={(event) => followLink(event, "/")}>
      <span class="block size-3 rounded-full bg-accent"></span>Darkroom
    </a>
    <nav aria-label="Main">
      <ul class="flex flex-col gap-0.5">
        {#each PAGES as item (item.path)}
          <li>
            <a
              href={item.path}
              aria-current={page?.path === item.path ? "page" : undefined}
              class="flex min-h-10 items-center rounded-md px-3 font-medium hover:bg-raised aria-[current=page]:bg-ink aria-[current=page]:text-surface"
              onclick={(event) => followLink(event, item.path)}
            >{item.label}</a>
          </li>
        {/each}
      </ul>
    </nav>
    <div class="mt-auto flex flex-col gap-3 p-3 text-sm text-ink-muted">
      <p>Signed in as<br /><b class="text-ink" data-testid="signed-in-as">{displayName(me)}</b></p>
      {@render signOut()}
    </div>
  </aside>

  <div class="flex min-w-0 flex-1 flex-col">
    <header class="flex min-h-14 items-center justify-between border-b border-line bg-rail px-4 py-1.5 md:hidden">
      <a href="/" class="flex items-center gap-2.5 text-lg font-bold" onclick={(event) => followLink(event, "/")}>
        <span class="block size-3 rounded-full bg-accent"></span>Darkroom
      </a>
      <Popover.Root>
        <Popover.Trigger class="btn min-w-11 px-3" aria-label="Account: {displayName(me)}">{initial}</Popover.Trigger>
        <Popover.Portal>
          <Popover.Content align="end" sideOffset={8} class="z-50 flex w-64 flex-col gap-3 rounded-lg border border-line bg-surface p-4 text-sm">
            <p class="text-ink-muted">Signed in as<br /><b class="break-words text-ink">{displayName(me)}</b></p>
            {@render signOut()}
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </header>

    <main class="min-w-0 flex-1 px-4 pt-5 pb-24 md:px-10 md:py-8">
      {#if page?.path === "/"}
        <Status />
      {:else if page?.path === "/people"}
        <People />
      {:else if page?.path === "/imports"}
        <Imports />
      {:else if page?.path === "/review"}
        <Review />
      {:else if page?.path === "/exports"}
        <Exports />
      {:else if page?.path === "/browser"}
        <Browser />
      {:else if page}
        <Placeholder title={page.label} />
      {:else}
        <Placeholder title="Page not found" note="There is no page at this address." />
      {/if}
    </main>

    <nav aria-label="Main tabs" class="fixed inset-x-0 bottom-0 z-30 border-t border-line bg-rail md:hidden">
      <ul class="grid grid-cols-6">
        {#each PAGES as item (item.path)}
          <li>
            <a
              href={item.path}
              aria-current={page?.path === item.path ? "page" : undefined}
              class="flex min-h-14 items-center justify-center text-xs font-semibold aria-[current=page]:bg-ink aria-[current=page]:text-surface focus-visible:-outline-offset-3"
              onclick={(event) => followLink(event, item.path)}
            >{item.label}</a>
          </li>
        {/each}
      </ul>
    </nav>
  </div>
</div>
