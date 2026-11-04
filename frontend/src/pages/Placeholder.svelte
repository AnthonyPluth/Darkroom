<script lang="ts">
  let { title, note = "This page arrives in a later issue." }: { title: string; note?: string } = $props();
</script>

<div class="flex flex-col gap-1">
  <h1 class="text-xl font-bold tracking-tight">{title}</h1>
  <p class="text-ink-muted">{note}</p>
</div>
