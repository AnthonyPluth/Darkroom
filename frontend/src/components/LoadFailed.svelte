<script lang="ts">
  let { message, retry }: { message: string; retry: () => void } = $props();
</script>

<div role="alert" class="card border-danger">
  <p>{message}</p>
  <button type="button" class="btn self-start" onclick={retry}>Try again</button>
</div>
