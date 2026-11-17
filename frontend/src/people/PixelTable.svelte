<script lang="ts">
  import type { PixelView } from "$lib/api-types";
  import { ago, names } from "$lib/format";
  import { pixelSyncthing } from "$lib/view";
  import Chip from "../components/Chip.svelte";
  import Table from "../components/Table.svelte";
  import { personName, type Family } from "./family";

  let { data, empty, onedit, onpair, onshare }: {
    data: Family;
    empty: string;
    onedit: (pixel: PixelView) => void;
    onpair?: (pixel: PixelView) => void;
    onshare?: (pixel: PixelView) => void;
  } = $props();

  const syncthing = $derived(data.transport?.syncthing ?? null);
</script>

<Table label="Pixels" columns={syncthing === null ? ["Device", "Google account", "People served", "Last sync", ""] : ["Device", "Google account", "People served", "Syncthing", "Last sync", ""]} rows={data.pixels} {empty}>
  {#snippet row(pixel)}
    {@const transport = syncthing?.find((t) => t.pixel_id === pixel.id)}
    {@const state = transport === undefined ? null : pixelSyncthing(transport, data.now)}
    <tr>
      <td data-label="Device"><b>{pixel.name}</b></td>
      <td data-label="Google account">{data.accounts.find((a) => a.id === pixel.google_account_id)?.email ?? "Unknown"}</td>
      <td data-label="People served">{names(pixel.people_ids.map((id) => personName(data.people, id)), "No one")}</td>
      {#if syncthing !== null}
        <td data-label="Syncthing">
          {#if state !== null}
            <span class="flex flex-col items-start gap-1"><Chip tone={state.tone} label={state.label} />
              {#each state.lines as line (line)}<span class="text-sm text-ink-muted">{line}</span>{/each}
            </span>
          {/if}
        </td>
      {/if}
      <td data-label="Last sync" class:text-ink-muted={pixel.last_sync === null}>{pixel.last_sync === null ? "Not synced yet" : ago(pixel.last_sync, data.now)}</td>
      {#if data.transport === null}
        <td class="md:text-right"><button type="button" class="btn" aria-label="Edit {pixel.name}" onclick={() => onedit(pixel)}>Edit</button></td>
      {:else}
        <td class="md:text-right">
          <span class="flex flex-wrap justify-end gap-2 md:flex-nowrap">
            {#if data.transport.pairing && !pixel.syncthing_device_id}
              <button type="button" class="btn" aria-label="Pair {pixel.name}" onclick={() => onpair?.(pixel)}>Pair</button>
            {:else if transport?.state === "device_id_invalid" || transport?.state === "device_id_shared"}
              <button type="button" class="btn" aria-label="Pair {pixel.name} again" onclick={() => onpair?.(pixel)}>Pair again</button>
            {:else if transport?.state === "folder_removed"}
              <button type="button" class="btn" aria-label="Share {pixel.name}'s folder again" onclick={() => onshare?.(pixel)}>Share again</button>
            {/if}
            <button type="button" class="btn" aria-label="Edit {pixel.name}" onclick={() => onedit(pixel)}>Edit</button>
          </span>
        </td>
      {/if}
    </tr>
  {/snippet}
</Table>
