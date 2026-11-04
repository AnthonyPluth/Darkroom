<script lang="ts">
  import { act } from "$lib/act";
  import FormError from "../forms/FormError.svelte";
  import Modal from "./Modal.svelte";

  let { open = $bindable(false), title, message, confirm, run, done }: {
    open?: boolean;
    title: string;
    message: string;
    confirm: string;
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
    const ok = await act(run, { busy: (on) => (busy = on), onError: (m) => (error = m) });
    if (ok) done();
  }
</script>

<Modal bind:open {title} description={message}>
  <div class="flex flex-col gap-4">
    <FormError message={error} />
    <div class="flex justify-end gap-3">
      <button type="button" class="btn" onclick={() => (open = false)}>Cancel</button>
      <button type="button" class="btn btn-danger" disabled={busy} onclick={go}>{confirm}</button>
    </div>
  </div>
</Modal>
