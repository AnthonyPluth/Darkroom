<script lang="ts">
  import { tick } from "svelte";
  import type { HoldResolveBody, HoldView } from "$lib/api-types";
  import { ApiError } from "$lib/api";
  import { act } from "$lib/act";
  import { apiCall } from "$lib/contract";
  import { actionLabels, formLabels, holdFacts, holdLabels, holdSource, isFormAction, isHoldAction, offsetMinutes, resolutionText, tomorrowDate, type FormAction, type HoldAction } from "$lib/holds";
  import Chip from "./Chip.svelte";
  import FormError from "../forms/FormError.svelte";

  let { row, person, refresh }: {
    row: HoldView; person: string; refresh: (message?: string) => Promise<void>;
  } = $props();

  const offsetError = "Enter a UTC offset from −12:00 to +14:00 in 15-minute steps.";
  const captureError = "Enter a date from 1 Jan 1900 through tomorrow, a local time, and its UTC offset.";
  let editing = $state<FormAction | null>(null);
  let sign = $state("");
  let hours = $state<number>();
  let minutes = $state("");
  let date = $state("");
  let time = $state("");
  let busy = $state(false);
  let error = $state("");
  let form = $state<HTMLFormElement>();
  let trigger: HTMLButtonElement | null = null;

  const facts = $derived(holdFacts(row));
  const available = $derived(row.actions.filter(isHoldAction));
  const resolution = $derived(row.resolution);
  const message = $derived(resolution ? resolutionText(resolution) : null);
  const decided = $derived(resolution?.state === "recorded" || resolution?.state === "applied");
  const name = $derived(row.name ?? "Unknown file");

  async function open(action: FormAction, button: HTMLButtonElement): Promise<void> {
    trigger = button;
    sign = minutes = date = time = error = "";
    hours = undefined;
    editing = action;
    await tick();
    form?.querySelector<HTMLElement>("input, select")?.focus();
  }

  async function cancel(): Promise<void> {
    editing = null;
    error = "";
    await tick();
    trigger?.focus();
  }

  async function record(body?: HoldResolveBody): Promise<void> {
    if (busy) return;
    error = "";
    busy = true;
    try {
      let conflict = "";
      const ok = await act(() => body
        ? apiCall<"POST /api/holds/{id}/resolve">(`/api/holds/${row.id}/resolve`, { method: "POST", body })
        : apiCall<"POST /api/holds/{id}/undo">(`/api/holds/${row.id}/undo`, { method: "POST" }), {
        onError: (reason, failure) => {
          if (failure instanceof ApiError && [404, 409].includes(failure.status)) conflict = reason;
          else error = reason;
        },
      });
      if (ok || conflict) {
        editing = null;
        await refresh(conflict);
      }
    } finally {
      busy = false;
    }
  }

  function choose(action: HoldAction, button: HTMLButtonElement): void {
    if (isFormAction(action)) void open(action, button);
    else void record({ action });
  }

  function submit(event: SubmitEvent): void {
    event.preventDefault();
    if (editing === null || busy) return;
    const offset = offsetMinutes(sign, hours, minutes);
    if (editing === "capture_time") {
      if (offset === null || !date || date < "1900-01-01" || date > tomorrowDate() || !time) {
        error = captureError;
        return;
      }
      void record({ action: editing, offset_minutes: offset, taken_local: `${date}T${time.length === 5 ? `${time}:00` : time}` });
    } else if (offset === null) {
      error = offsetError;
    } else {
      void record({ action: editing, offset_minutes: offset });
    }
  }
</script>

<article aria-labelledby="held-name-{row.id}" class="grid min-w-0 gap-3 border-t border-line py-4 first-of-type:border-t-0 first-of-type:pt-0 md:grid-cols-[minmax(0,14rem)_minmax(0,1fr)] md:gap-4">
  <div class="min-w-0">
    <h3 id="held-name-{row.id}" class="font-mono font-semibold break-all">{name}</h3>
    <p class="text-sm text-ink-muted">{person} · {holdSource(row)}</p>
  </div>
  <div class="flex min-w-0 flex-col gap-3">
    <div class="self-start"><Chip tone="warn" label={holdLabels[row.hold]} /></div>
    <p>{row.reason}</p>
    {#if facts.length}
      <dl class="grid grid-cols-1 gap-x-3 md:grid-cols-[8.25rem_minmax(0,1fr)] md:gap-y-1">
        {#each facts as [label, value] (label)}
          <dt class="text-sm text-ink-muted">{label}</dt>
          <dd class="mb-1 min-w-0 wrap-break-word md:mb-0">{value}</dd>
        {/each}
      </dl>
    {/if}
    {#if message}
      <p role="status">{message}</p>
    {/if}
    {#if decided}
      {#if resolution?.can_undo}
        <button type="button" class="btn self-start" disabled={busy} onclick={() => record()}>Undo</button>
      {:else}
        <p class="text-sm text-ink-muted">Already sent to Immich, so it can’t be undone.</p>
      {/if}
    {:else}
      {#if editing !== null}
        <form class="flex flex-col gap-3 rounded-md border border-line bg-raised p-3" bind:this={form} onsubmit={submit} novalidate>
          <fieldset class="flex min-w-0 flex-col gap-2" disabled={busy} aria-describedby={error ? `held-error-${row.id}` : undefined}>
            <legend class="font-semibold">{actionLabels[editing]}</legend>
            {#if editing === "capture_time"}
              <div class="grid grid-cols-2 gap-2">
                <label class="label">Date <input type="date" class="input" bind:value={date} min="1900-01-01" max={tomorrowDate()} required /></label>
                <label class="label">Local time <input type="time" class="input" bind:value={time} step="1" required /></label>
              </div>
            {/if}
            <div class="grid grid-cols-3 gap-2">
              <label class="label">Sign
                <select class="input" bind:value={sign} required>
                  <option value="">Choose</option>
                  <option value="+">+</option>
                  <option value="-">−</option>
                </select>
              </label>
              <label class="label">Hours
                <input type="number" inputmode="numeric" class="input" bind:value={hours} min="0" max="14" step="1" required />
              </label>
              <label class="label">Minutes
                <select class="input" bind:value={minutes} required>
                  <option value="">Choose</option>
                  {#each ["00", "15", "30", "45"] as value (value)}
                    <option {value}>{value}</option>
                  {/each}
                </select>
              </label>
            </div>
            <p class="text-sm text-ink-muted">UTC−12:00 to UTC+14:00, in 15-minute steps.</p>
          </fieldset>
          <div class="flex flex-col gap-2 sm:flex-row">
            <button type="submit" class="btn btn-primary" disabled={busy}>{formLabels[editing]}</button>
            <button type="button" class="btn" disabled={busy} onclick={cancel}>Cancel</button>
          </div>
        </form>
      {/if}
      <div class="flex flex-col flex-wrap gap-2 sm:flex-row">
        {#each available as action (action)}
          <button type="button" class="btn" class:btn-primary={(isFormAction(action) && action !== "record_offset") || action === "drop_description"}
            class:hidden={action === editing} disabled={busy} onclick={(event) => choose(action, event.currentTarget)}>{actionLabels[action]}</button>
        {/each}
      </div>
    {/if}
    <div id="held-error-{row.id}">
      <FormError message={error} />
    </div>
  </div>
</article>
