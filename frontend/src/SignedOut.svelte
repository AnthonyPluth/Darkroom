<script lang="ts">
  import { Button } from "bits-ui";
  import { signInUrl } from "$lib/api";
</script>

<main class="mx-auto flex min-h-dvh max-w-sm flex-col justify-center gap-4 px-4" data-placeholder="design from #16">
  <h1 class="text-2xl font-semibold">Darkroom</h1>
  <p>Sign in to see your household's photos.</p>
  <Button.Root href={signInUrl()} class="self-start rounded-md bg-placeholder-accent px-4 py-2 text-placeholder-accent-ink">Sign in</Button.Root>
  <p class="text-sm text-placeholder-ink-muted">This sign-in screen is a placeholder until the signed-off design arrives.</p>
</main>
