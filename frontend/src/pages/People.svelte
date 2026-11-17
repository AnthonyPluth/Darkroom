<script lang="ts">
  import { onMount } from "svelte";
  import { Loader } from "$lib/loader.svelte";
  import { followLink } from "$lib/nav.svelte";
  import LoadFailed from "../components/LoadFailed.svelte";
  import AccountTable from "../people/AccountTable.svelte";
  import { loadFamily } from "../people/family";
  import FamilyEditor from "../people/FamilyEditor.svelte";
  import PersonTable from "../people/PersonTable.svelte";
  import PixelTable from "../people/PixelTable.svelte";

  const loader = new Loader(() => loadFamily({ transport: true }));
  let editor: FamilyEditor | undefined = $state();

  onMount(() => void loader.load());
</script>

<div class="flex flex-col gap-7">
  <div class="flex flex-col gap-1">
    <h1 class="text-xl font-bold tracking-tight">People</h1>
    <p class="text-ink-muted">Who the family is, and which Google accounts and Pixels serve them.</p>
  </div>

  {#if loader.current.kind === "loading"}
    <p role="status" class="text-ink-muted">Loading...</p>
  {:else if loader.current.kind === "error"}
    <LoadFailed message={loader.current.message} retry={() => loader.load()} />
  {:else}
    {@const data = loader.current.data}
    <section class="card" aria-label="Persons">
      <div class="flex items-center justify-between gap-3">
        <h2 class="text-md font-semibold">Persons</h2>
        <button type="button" class="btn" onclick={() => editor?.edit({ kind: "person", item: null })}>Add person</button>
      </div>
      <PersonTable {data} empty="No one has been added yet." onedit={(item) => editor?.edit({ kind: "person", item })} />
    </section>

    <section class="card" aria-label="Google accounts">
      <div class="flex items-center justify-between gap-3">
        <h2 class="text-md font-semibold">Google accounts</h2>
        <button type="button" class="btn" disabled={data.people.length === 0} onclick={() => editor?.edit({ kind: "account", item: null })}>Add account</button>
      </div>
      <AccountTable
        {data}
        empty={data.people.length === 0 ? "Add a person first." : "No Google accounts yet."}
        signIn={{ href: "/browser", onclick: (e) => followLink(e, "/browser") }}
        onedit={(item) => editor?.edit({ kind: "account", item })}
      />
    </section>

    <section class="card" aria-label="Pixels">
      <div class="flex items-center justify-between gap-3">
        <h2 class="text-md font-semibold">Pixels</h2>
        <button type="button" class="btn" disabled={data.accounts.length === 0} onclick={() => editor?.edit({ kind: "pixel", item: null })}>Add Pixel</button>
      </div>
      <PixelTable
        {data}
        empty={data.accounts.length === 0 ? "Add a Google account first." : "No Pixels yet."}
        onedit={(item) => editor?.edit({ kind: "pixel", item })}
        onpair={(item) => editor?.pair(item)}
        onshare={(item) => editor?.shareAgain(item)}
      />
    </section>

    <FamilyEditor bind:this={editor} {data} onchanged={() => void loader.load(true)} />
  {/if}
</div>
