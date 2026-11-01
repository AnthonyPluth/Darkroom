<script lang="ts">
  import { Button } from "bits-ui";

  let { message, retry }: { message: string; retry: () => void } = $props();
</script>

<main class="mx-auto flex min-h-dvh max-w-sm flex-col justify-center gap-4 px-4">
  <h1 class="text-2xl font-semibold">Darkroom</h1>
  <p role="alert">{message}</p>
  <Button.Root type="button" class="self-start rounded-md border border-placeholder-line px-4 py-2" onclick={retry}>Try again</Button.Root>
</main>
