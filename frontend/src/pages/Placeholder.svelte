<script lang="ts">
  let { title, note = "This page arrives in a later issue." }: { title: string; note?: string } = $props();
</script>

<h1>{title}</h1>
<p>{note}</p>
