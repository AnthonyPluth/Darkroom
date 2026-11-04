<script lang="ts">
  let { message }: { message: string } = $props();
</script>

{#if message}
  <p role="alert" class="rounded-md border border-danger bg-danger-bg px-3 py-2.5 text-danger">{message}</p>
{/if}
