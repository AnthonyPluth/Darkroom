<script lang="ts" generics="T">
  import type { Snippet } from "svelte";

  let { label, columns, rows, row, empty }: { label: string; columns: string[]; rows: T[]; row: Snippet<[T]>; empty: string } = $props();
</script>

{#if rows.length === 0}
  <p class="text-ink-muted">{empty}</p>
{:else}
  <table class="grid-table" aria-label={label}>
    <thead>
      <tr>
        {#each columns as column (column)}
          <th scope="col" class="label">{column}</th>
        {/each}
      </tr>
    </thead>
    <tbody>
      {#each rows as item, index (index)}
        {@render row(item)}
      {/each}
    </tbody>
  </table>
{/if}
