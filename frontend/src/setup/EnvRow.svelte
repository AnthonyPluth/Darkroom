<script lang="ts">
  import { ENVIRONMENT_HINT, ENVIRONMENT_ROW } from "./wizard";

  let { label }: { label: string } = $props();
</script>

<div class="flex flex-col gap-0.5 rounded-md border border-line p-3">
  <b class="font-semibold">{label}</b>
  <span>{ENVIRONMENT_ROW}</span>
  <span class="text-sm text-ink-muted">{ENVIRONMENT_HINT}</span>
</div>
