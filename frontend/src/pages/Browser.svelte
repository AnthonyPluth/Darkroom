<script lang="ts">
  import { onMount } from "svelte";
  import { apiCall } from "$lib/contract";
  import { nowSeconds } from "$lib/format";
  import { Loader } from "$lib/loader.svelte";
  import { workerChip } from "$lib/view";
  import Chip from "../components/Chip.svelte";
  import LoadFailed from "../components/LoadFailed.svelte";

  const REFRESH_MS = 10_000;

  const loader = new Loader(async () => {
    const [view, accounts, jobs] = await Promise.all([
      apiCall<"GET /api/browser/view">("/api/browser/view", { failed: "Couldn't load the remote browser" }),
      apiCall<"GET /api/accounts">("/api/accounts", { failed: "Couldn't load the Google accounts" }),
      apiCall<"GET /api/browser/jobs">("/api/browser/jobs", { failed: "Couldn't load the browser jobs" }),
    ]);
    return { view, accounts: accounts.accounts, jobs: jobs.jobs, now: nowSeconds() };
  });

  onMount(() => {
    void loader.load();
    const timer = setInterval(() => void loader.load(true), REFRESH_MS);
    return () => clearInterval(timer);
  });

  function host(address: string): string {
    return new URL(address).host;
  }
</script>

<div class="flex flex-col gap-5">
  {#if loader.current.kind === "loading"}
    <h1 class="text-xl font-bold tracking-tight">Remote browser</h1>
    <p role="status" class="text-ink-muted">Loading...</p>
  {:else if loader.current.kind === "error"}
    <h1 class="text-xl font-bold tracking-tight">Remote browser</h1>
    <LoadFailed message={loader.current.message} retry={() => loader.load()} />
  {:else}
    {@const data = loader.current.data}
    {@const chip = workerChip(data.view.worker_last_seen, data.now)}
    {@const waiting = data.accounts.filter((a) => data.jobs.some((j) => j.account_id === a.id && j.status === "waiting_for_user"))}
    <div class="flex flex-wrap items-end justify-between gap-4">
      <div class="flex flex-col gap-1">
        <h1 class="text-xl font-bold tracking-tight">Remote browser</h1>
        <p class="text-ink-muted">A browser running inside Darkroom, used to sign in to Google.</p>
      </div>
      <Chip tone={chip.tone} label={chip.label} />
    </div>

    {#each waiting as account (account.id)}
      {@const reason = data.jobs.find((j) => j.account_id === account.id && j.status === "waiting_for_user")?.message}
      <section class="flex flex-col gap-2.5 rounded-lg border border-accent bg-surface p-4 md:flex-row md:items-center md:justify-between md:gap-5 md:px-5" aria-label="Sign-in task">
        <div class="flex flex-col gap-1">
          <div class="flex flex-wrap items-center gap-2.5">
            <Chip tone="bad" label={reason === "wrong_account" ? "Wrong account" : "Sign-in needed"} />
            <b class="text-md">{reason === "wrong_account" ? `Switch to ${account.email}` : `Sign in to ${account.email}`}</b>
          </div>
          <p>
            {#if reason === "wrong_account"}
              The browser is signed in to a different Google account. Sign out of it in the window below and sign in as {account.email}.
            {:else}
              {reason === "verify" ? "Google asked to verify this account." : "This account needs a Google sign-in."}
              Sign in in the window below as you normally would and finish any verification.
            {/if}
            When you can see the Google Takeout page, Darkroom notices within a minute, keeps the session and carries on.
          </p>
        </div>
      </section>
    {/each}

    <section class="flex flex-col overflow-hidden rounded-lg border border-line bg-raised" aria-label="Remote browser view">
      {#if data.view.vnc_url}
        <div class="bg-canvas px-3 py-2 text-xs text-ink-muted"><span class="rounded-full bg-raised px-3 py-1 font-mono text-ink-muted">{host(data.view.vnc_url)}</span></div>
        <iframe title="Remote browser" src={data.view.vnc_url} class="h-90 w-full border-0 md:h-130"></iframe>
      {:else}
        <div class="flex h-90 flex-col items-center justify-center gap-2 p-5 text-center md:h-130">
          <p class="text-md font-semibold">The remote browser is not set up</p>
          <p class="max-w-105 text-ink-muted">Set VNC_URL to the address of the browser worker's noVNC view and restart Darkroom.</p>
        </div>
      {/if}
    </section>
    <p class="text-sm text-ink-muted">This view needs Darkroom reachable on home Wi-Fi or Tailscale.</p>
  {/if}
</div>
