<script lang="ts">
  import { signInUrl } from "$lib/api";
</script>

<div class="grid min-h-dvh md:grid-cols-2">
  <div class="flex flex-col gap-8 px-4 pt-6 md:justify-between md:gap-0 md:bg-rail md:p-16">
    <div class="flex items-center gap-2.5 text-lg font-bold md:gap-3">
      <span class="block size-3.5 rounded-full bg-accent"></span>Darkroom
    </div>
    <div class="mt-12 flex max-w-105 flex-col gap-3 md:mt-0 md:gap-4">
      <p class="text-3xl leading-tight font-bold tracking-tight md:text-4xl">Photos leave the phone only after Google Photos has them.</p>
      <p class="text-md text-ink-muted">Darkroom syncs the family's iPhones to the Pixels, imports Google Takeout, and keeps one copy of everything.</p>
    </div>
    <div class="hidden gap-1.5 md:flex" aria-hidden="true">
      <span class="block h-24 w-18 rounded-sm bg-line"></span>
      <span class="block size-24 rounded-sm bg-[#4a4439]"></span>
      <span class="block h-24 w-18 rounded-sm bg-raised"></span>
    </div>
  </div>
  <main class="flex items-start justify-center px-4 pt-8 pb-8 md:items-center md:p-12">
    <div class="card w-full max-w-100 gap-4 p-5 md:gap-5 md:p-8">
      <div class="flex flex-col gap-1.5">
        <h1 class="text-xl font-bold md:text-xl">Sign in</h1>
        <p class="text-ink-muted">You are signed out. Darkroom is for the family's parents only.</p>
      </div>
      <a class="btn btn-primary" href={signInUrl()}>Continue with Pocket ID</a>
      <p class="text-sm text-ink-muted">Pocket ID will ask you to sign in, then bring you back here. Reaching Darkroom away from home needs Tailscale to be on.</p>
    </div>
  </main>
</div>
