<script lang="ts">
  let { checked, label, name, disabled = false, onclick }: {
    checked: boolean;
    label: string;
    name: string;
    disabled?: boolean;
    onclick: () => void;
  } = $props();
</script>

<button type="button" role="switch" aria-checked={checked} aria-label={name} {disabled} {onclick}
  class="inline-flex min-h-11 cursor-pointer items-center gap-2.5 self-start border-0 bg-transparent p-0 font-semibold text-ink disabled:cursor-not-allowed disabled:opacity-60">
  <span class="flex h-[26px] w-[46px] items-center rounded-full border p-0.5 {checked ? 'justify-end border-ink bg-ink' : 'border-line-strong bg-raised'}">
    <b class="block size-5 rounded-full {checked ? 'bg-surface' : 'bg-ink-muted'}"></b>
  </span>
  {label}
</button>
