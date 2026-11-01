<script lang="ts">
  import { Button } from "bits-ui";
  import type { Me } from "$lib/api-types";
  import { followLink, pageFor, PAGES, router } from "$lib/nav.svelte";
  import { displayName } from "$lib/session.svelte";
  import Placeholder from "./pages/Placeholder.svelte";

  let { me }: { me: Me } = $props();
  const page = $derived(pageFor(router.path));
</script>

<div class="min-h-dvh">
  <header class="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-placeholder-line bg-placeholder-surface px-4 py-3">
    <a href="/" class="font-semibold" onclick={(event) => followLink(event, "/")}>Darkroom</a>
    <nav aria-label="Main" class="order-last w-full md:order-none md:w-auto">
      <ul class="flex flex-wrap gap-1">
        {#each PAGES as item (item.path)}
          <li>
            <a
              href={item.path}
              aria-current={page?.path === item.path ? "page" : undefined}
              class="block rounded-md px-2.5 py-1.5 text-sm text-placeholder-ink-muted hover:text-placeholder-ink aria-[current=page]:bg-placeholder-canvas aria-[current=page]:text-placeholder-ink"
              onclick={(event) => followLink(event, item.path)}
            >{item.label}</a>
          </li>
        {/each}
      </ul>
    </nav>
    <div class="ml-auto flex min-w-0 items-center gap-3">
      <span class="truncate text-sm text-placeholder-ink-muted" data-testid="signed-in-as">{displayName(me)}</span>
      <form method="post" action="/auth/logout">
        <Button.Root type="submit" class="rounded-md border border-placeholder-line px-3 py-1.5 text-sm">Sign out</Button.Root>
      </form>
    </div>
  </header>
  <main class="px-4 py-6">
    {#if page}
      <Placeholder title={page.label} />
    {:else}
      <Placeholder title="Page not found" note="There is no page at this address." />
    {/if}
  </main>
</div>
