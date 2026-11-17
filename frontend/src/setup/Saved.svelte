<script lang="ts">
  let { message }: { message: string } = $props();
</script>

<p role="status" class="flex items-center gap-2"><span class="block size-2 flex-none rounded-full bg-ok"></span>{message}</p>
