<script lang="ts">
  import type { PixelView } from "$lib/api-types";
  import { ago, names } from "$lib/format";
  import Table from "../components/Table.svelte";
  import { personName, type Family } from "./family";

  let { data, empty, onedit }: { data: Family; empty: string; onedit: (pixel: PixelView) => void } = $props();
</script>

<Table label="Pixels" columns={["Device", "Google account", "People served", "Last sync", ""]} rows={data.pixels} {empty}>
  {#snippet row(pixel)}
    <tr>
      <td data-label="Device"><b>{pixel.name}</b></td>
      <td data-label="Google account">{data.accounts.find((a) => a.id === pixel.google_account_id)?.email ?? "Unknown"}</td>
      <td data-label="People served">{names(pixel.people_ids.map((id) => personName(data.people, id)), "No one")}</td>
      <td data-label="Last sync" class:text-ink-muted={pixel.last_sync === null}>{pixel.last_sync === null ? "Not synced yet" : ago(pixel.last_sync, data.now)}</td>
      <td class="md:text-right"><button type="button" class="btn" aria-label="Edit {pixel.name}" onclick={() => onedit(pixel)}>Edit</button></td>
    </tr>
  {/snippet}
</Table>
