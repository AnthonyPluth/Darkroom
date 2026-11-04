<script lang="ts">
  import type { Snippet } from "svelte";

  let { label, hint, children }: { label: string; hint?: string; children: Snippet<[{ id: string; describedby: string | undefined }]> } = $props();
  const id = $props.id();
  const hintId = `${id}-hint`;
</script>

<div class="flex flex-col gap-1.5">
  <label for={id} class="font-semibold">{label}</label>
  {@render children({ id, describedby: hint ? hintId : undefined })}
  {#if hint}<span id={hintId} class="text-sm text-ink-muted">{hint}</span>{/if}
</div>
