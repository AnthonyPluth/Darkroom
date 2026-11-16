<script lang="ts">
  import { act } from "$lib/act";
  import FormError from "../forms/FormError.svelte";
  import Modal from "./Modal.svelte";

  let { open = $bindable(false), title, message, detail, more = [], confirm, primary = false, refused = (m: string) => m, run, done }: {
    open?: boolean;
    title: string;
    message: string;
    detail?: string;
    more?: string[];
    confirm: string;
    primary?: boolean;
    refused?: (message: string) => string;
    run: () => Promise<unknown>;
    done: () => void;
  } = $props();

  let busy = $state(false);
  let error = $state("");

  $effect(() => {
    if (open) error = "";
  });

  async function go(): Promise<void> {
    error = "";
    const ok = await act(run, { busy: (on) => (busy = on), onError: (m) => (error = refused(m)) });
    if (ok) done();
  }
</script>

<Modal bind:open {title} description={message} dismissible={!busy}>
  <div class="flex flex-col gap-4">
    {#if detail}
      <p>{detail}</p>
    {/if}
    {#each more as paragraph (paragraph)}<p class="text-ink-muted">{paragraph}</p>{/each}
    <FormError message={error} />
    <div class="flex justify-end gap-3">
      <button type="button" class="btn" disabled={busy} onclick={() => (open = false)}>Cancel</button>
      <button type="button" class="btn {primary ? 'btn-primary' : 'btn-danger'}" disabled={busy} onclick={go}>{confirm}</button>
    </div>
  </div>
</Modal>
