<script lang="ts">
  import { onMount } from "svelte";
  import { onSetupPage, router, watchHistory } from "$lib/nav.svelte";
  import { session, watchSignOut } from "$lib/session.svelte";
  import Shell from "./Shell.svelte";
  import SignedOut from "./SignedOut.svelte";
  import Setup from "./setup/Setup.svelte";
  import Unreachable from "./Unreachable.svelte";

  const setupPage = onSetupPage(router.path);

  onMount(() => {
    const stopSignOut = watchSignOut();
    const stopHistory = watchHistory();
    if (!setupPage) void session.load();
    return () => { stopSignOut(); stopHistory(); };
  });
</script>

{#if setupPage}
  <Setup />
{:else if session.current.kind === "loading"}
  <p role="status" class="p-4 text-ink-muted">Loading...</p>
{:else if session.current.kind === "signed-out"}
  <SignedOut />
{:else if session.current.kind === "unreachable"}
  <Unreachable message={session.current.message} retry={() => session.load()} />
{:else}
  <Shell me={session.current.me} />
{/if}
