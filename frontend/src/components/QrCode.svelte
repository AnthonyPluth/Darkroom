<script lang="ts">
  import { qrMatrix } from "$lib/qr";

  let { text, label }: { text: string; label: string } = $props();

  const QUIET = 4;
  const matrix = $derived(qrMatrix(text));
  const side = $derived(matrix.length + QUIET * 2);
  const dark = $derived(matrix.flatMap((row, y) => row.map((on, x) => (on ? `M${x + QUIET} ${y + QUIET}h1v1h-1z` : "")).filter(Boolean)).join(""));
</script>

<svg role="img" aria-label={label} viewBox="0 0 {side} {side}" shape-rendering="crispEdges" class="block size-[224px] self-center rounded-sm md:size-[192px]" data-modules={matrix.length}>
  <rect width={side} height={side} class="fill-ink" />
  <path d={dark} class="fill-canvas" />
</svg>
