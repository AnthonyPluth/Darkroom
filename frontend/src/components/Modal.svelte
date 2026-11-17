<script lang="ts">
  import { Dialog } from "bits-ui";
  import type { Snippet } from "svelte";

  let { open = $bindable(false), title, description, dismissible = true, wide = false, children }: { open?: boolean; title: string; description?: string; dismissible?: boolean; wide?: boolean; children: Snippet } = $props();
</script>

<Dialog.Root bind:open>
  <Dialog.Portal>
    <Dialog.Overlay class="fixed inset-0 z-40 bg-canvas/80" />
    <Dialog.Content escapeKeydownBehavior={dismissible ? "close" : "ignore"} interactOutsideBehavior={dismissible ? "close" : "ignore"} class={wide ? "fixed top-1/2 left-1/2 z-50 max-h-[90dvh] w-[calc(100%-2rem)] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border border-line bg-surface p-5 md:right-10 md:left-68 md:w-auto md:translate-x-0 md:p-6" : "fixed top-1/2 left-1/2 z-50 max-h-[90dvh] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border border-line bg-surface p-5 md:p-6"}>
      <Dialog.Title class="text-lg font-bold">{title}</Dialog.Title>
      {#if description}
        <Dialog.Description class="mt-1 text-ink-muted">{description}</Dialog.Description>
      {:else}
        <Dialog.Description class="sr-only">{title}</Dialog.Description>
      {/if}
      <div class="mt-4">{@render children()}</div>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
