<script lang="ts">
  import type { PersonView } from "$lib/api-types";
  import { names } from "$lib/format";
  import Table from "../components/Table.svelte";
  import { pixelLabel, type Family } from "./family";

  let { data, empty, onedit }: { data: Family; empty: string; onedit: (person: PersonView) => void } = $props();
</script>

<Table label="Persons" columns={["Person", "Immich user", "Google account", "Pixel", ""]} rows={data.people} {empty}>
  {#snippet row(person)}
    <tr>
      <td data-label="Person"><b>{person.name}</b></td>
      <td data-label="Immich user">{person.immich_user_id ?? "Not linked"}<br /><span class="text-ink-muted">{person.has_immich_key ? "API key saved" : "No API key"}</span></td>
      <td data-label="Google account">{names(data.accounts.filter((a) => a.served_people.includes(person.id)).map((a) => a.email), "None")}</td>
      <td data-label="Pixel">{names(data.pixels.filter((p) => p.people_ids.includes(person.id)).map((p) => pixelLabel(p, person, data.people)), "None")}</td>
      <td class="md:text-right"><button type="button" class="btn" aria-label="Edit {person.name}" onclick={() => onedit(person)}>Edit</button></td>
    </tr>
  {/snippet}
</Table>
