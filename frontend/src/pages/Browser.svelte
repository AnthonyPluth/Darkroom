<script lang="ts">
  import { onMount } from "svelte";
  import { act } from "$lib/act";
  import { apiCall } from "$lib/contract";
  import { nowSeconds } from "$lib/format";
  import { Loader } from "$lib/loader.svelte";
  import { workerChip } from "$lib/view";
  import Chip from "../components/Chip.svelte";
  import LoadFailed from "../components/LoadFailed.svelte";

  const REFRESH_MS = 10_000;
  let viewer: HTMLElement | undefined = $state();
  let frame: HTMLIFrameElement | undefined = $state();
  let frameFocused = $state(false);
  let fullscreen = $state(false);
  let canFullscreen = $state(false);
  let viewerError = $state("");

  const loader = new Loader(async () => {
    const [view, accounts, jobs] = await Promise.all([
      apiCall<"GET /api/browser/view">("/api/browser/view", { failed: "Couldn't load the remote browser" }),
      apiCall<"GET /api/accounts">("/api/accounts", { failed: "Couldn't load the Google accounts" }),
      apiCall<"GET /api/browser/jobs">("/api/browser/jobs", { failed: "Couldn't load the browser jobs" }),
    ]);
    return { view, accounts: accounts.accounts, jobs: jobs.jobs, now: nowSeconds() };
  });

  onMount(() => {
    canFullscreen = Boolean(document.fullscreenEnabled);
    const changed = () => { fullscreen = document.fullscreenElement === viewer; };
    const focusChanged = () => queueMicrotask(() => { frameFocused = Boolean(frame && document.activeElement === frame); });
    document.addEventListener("fullscreenchange", changed);
    document.addEventListener("focusin", focusChanged);
    document.addEventListener("focusout", focusChanged);
    window.addEventListener("focus", focusChanged);
    window.addEventListener("blur", focusChanged);
    void loader.load();
    const timer = setInterval(() => void loader.poll(), REFRESH_MS);
    return () => {
      clearInterval(timer);
      document.removeEventListener("fullscreenchange", changed);
      document.removeEventListener("focusin", focusChanged);
      document.removeEventListener("focusout", focusChanged);
      window.removeEventListener("focus", focusChanged);
      window.removeEventListener("blur", focusChanged);
    };
  });

  async function toggleFullscreen(): Promise<void> {
    if (!viewer) return;
    const target = viewer;
    viewerError = "";
    await act(() => fullscreen ? document.exitFullscreen() : target.requestFullscreen(), {
      onError: () => { viewerError = "Couldn't change full screen. Use Open in new tab to get a larger view."; },
    });
  }

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
      {@const job = data.jobs.find((j) => j.account_id === account.id && j.status === "waiting_for_user")}
      {@const reason = job?.message}
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
            When you can see the Google Takeout page, Darkroom confirms the account within a minute and keeps the session.
            {#if job?.kind === "connect"}
              After Connect finishes, use Create schedule on the Exports page to set up exports.
            {:else}
              The waiting browser job then carries on.
            {/if}
          </p>
        </div>
      </section>
    {/each}

    <section bind:this={viewer} class="browser-view flex flex-col overflow-hidden rounded-lg border border-line bg-raised" aria-label="Remote browser view">
      {#if data.view.vnc_url}
        <div class="flex flex-wrap items-center justify-between gap-3 bg-canvas px-3 py-2 text-xs text-ink-muted">
          <span class="min-w-0 break-all rounded-full bg-raised px-3 py-1 font-mono text-ink-muted">{host(data.view.vnc_url)}</span>
          <div class="flex flex-wrap items-center gap-2">
            {#if canFullscreen}
              <button type="button" class="btn min-h-13" aria-pressed={fullscreen} onclick={toggleFullscreen}>{fullscreen ? "Exit full screen" : "Full screen"}</button>
            {/if}
            <a class="btn min-h-13" href={data.view.vnc_url} target="_blank" rel="noopener noreferrer">Open in new tab</a>
          </div>
        </div>
        {#if viewerError}<p role="alert" class="px-3 py-2 text-danger">{viewerError}</p>{/if}
        <iframe bind:this={frame} title="Remote browser" aria-label="Remote browser" src={data.view.vnc_url} allowfullscreen class="browser-frame w-full border-0" class:viewer-focused={frameFocused}></iframe>
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

<style>
  .browser-frame {
    height: max(36rem, calc(100dvh - 18rem));
  }
  .browser-frame:focus, .browser-frame.viewer-focused {
    outline: 3px solid var(--color-ink);
    outline-offset: -3px;
  }
  .browser-view:fullscreen {
    width: 100%;
    height: 100%;
    border: 0;
    border-radius: 0;
  }
  .browser-view:fullscreen .browser-frame {
    flex: 1;
    min-height: 0;
    height: auto;
  }
</style>
