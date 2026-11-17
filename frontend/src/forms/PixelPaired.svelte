<script lang="ts">
  import Chip from "../components/Chip.svelte";

  let { name, ondone }: { name: string; ondone: () => void } = $props();
</script>

<div class="flex flex-col gap-4">
  <div><Chip tone="ok" label="Paired" /></div>
  <p>Darkroom is sharing {name}'s folder with it.</p>
  <ol class="flex list-decimal flex-col gap-1 pl-5">
    <li>On the Pixel, accept the folder Darkroom shares and choose where it goes.</li>
    <li>In Google Photos on the Pixel, turn on backup for that folder.</li>
  </ol>
  <p class="text-ink-muted">Darkroom shows the Pixel as connected once it has synced.</p>
  <div class="flex flex-wrap items-center justify-end gap-3">
    <button type="button" class="btn btn-primary" onclick={ondone}>Done</button>
  </div>
</div>
