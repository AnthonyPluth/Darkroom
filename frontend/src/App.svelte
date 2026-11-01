<script lang="ts">
  import { onMount } from "svelte";
  import { watchHistory } from "$lib/nav.svelte";
  import { session, watchSignOut } from "$lib/session.svelte";
  import Shell from "./Shell.svelte";
  import SignedOut from "./SignedOut.svelte";
  import Unreachable from "./Unreachable.svelte";

  onMount(() => {
    const stopSignOut = watchSignOut();
    const stopHistory = watchHistory();
    void session.load();
    return () => { stopSignOut(); stopHistory(); };
  });
</script>

{#if session.current.kind === "loading"}
  <p role="status" class="p-4 text-placeholder-ink-muted">Loading...</p>
{:else if session.current.kind === "signed-out"}
  <SignedOut />
{:else if session.current.kind === "unreachable"}
  <Unreachable message={session.current.message} retry={() => session.load()} />
{:else}
  <Shell me={session.current.me} />
{/if}
