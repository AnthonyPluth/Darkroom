<script lang="ts" module>
  export type Tone = "ok" | "warn" | "bad" | "neutral";
</script>

<script lang="ts">
  let { tone, label }: { tone: Tone; label: string } = $props();
</script>

<span
  class="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-semibold {tone === 'ok' ? 'bg-ok-bg text-ok' : tone === 'warn' ? 'bg-warn-bg text-warn' : tone === 'bad' ? 'bg-danger-bg text-danger' : 'bg-raised text-ink-muted'}"
  data-tone={tone}
>
  <i
    aria-hidden="true"
    class="block size-2 bg-current {tone === 'warn' ? 'rotate-45' : tone === 'bad' ? 'rounded-xs' : 'rounded-full'}"
  ></i>{label}
</span>
