<script lang="ts">
  let { message, retry }: { message: string; retry: () => void } = $props();
</script>

<main class="flex min-h-dvh items-start justify-center px-4 pt-12 md:items-center md:pt-0">
  <div class="card w-full max-w-100 gap-4 p-5 md:gap-5 md:p-8">
    <div class="flex items-center gap-2.5 text-lg font-bold">
      <span class="block size-3.5 rounded-full bg-accent"></span>Darkroom
    </div>
    <div role="alert" class="flex flex-col gap-1 rounded-md border border-danger bg-danger-bg px-4 py-3.5">
      <div class="flex items-center gap-2 font-bold text-danger"><span class="block size-2.5 rounded-xs bg-danger"></span>Can't reach Darkroom</div>
      <p>{message}</p>
    </div>
    <button type="button" class="btn btn-primary" onclick={retry}>Try again</button>
  </div>
</main>
