<script lang="ts">
  import type { AccountView } from "$lib/api-types";
  import { names } from "$lib/format";
  import { sessionChip } from "$lib/view";
  import Chip from "../components/Chip.svelte";
  import Table from "../components/Table.svelte";
  import { personName, type Family } from "./family";

  let { data, empty, signIn, onedit }: {
    data: Family;
    empty: string;
    signIn: { href: string; onclick?: (event: MouseEvent) => void };
    onedit: (account: AccountView) => void;
  } = $props();
</script>

<Table label="Google accounts" columns={["Account", "Default person", "Serves", "Session", ""]} rows={data.accounts} {empty}>
  {#snippet row(account)}
    {@const chip = sessionChip(account.session_state)}
    <tr>
      <td data-label="Account">{account.email}</td>
      <td data-label="Default person">{personName(data.people, account.default_person_id)}</td>
      <td data-label="Serves">{names(account.served_people.map((id) => personName(data.people, id)), "No one")}</td>
      <td data-label="Session"><Chip tone={chip.tone} label={chip.label} /></td>
      <td class="md:text-right">
        <span class="flex flex-wrap justify-end gap-2">
          {#if account.session_state === "needs_sign_in"}
            <a class="btn btn-primary" href={signIn.href} onclick={signIn.onclick}>Sign in</a>
          {/if}
          <button type="button" class="btn" aria-label="Edit {account.email}" onclick={() => onedit(account)}>Edit</button>
        </span>
      </td>
    </tr>
  {/snippet}
</Table>
