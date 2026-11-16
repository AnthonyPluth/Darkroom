<script lang="ts">
  import { onMount, tick } from "svelte";
  import { errMsg } from "$lib/act";
  import { ApiError, SIGNED_OUT } from "$lib/api";
  import type { SetupState } from "$lib/api-types";
  import { apiCall } from "$lib/contract";
  import FormError from "../forms/FormError.svelte";
  import { loadFamily, type Family } from "../people/family";
  import AddressStep from "./AddressStep.svelte";
  import AlertsStep from "./AlertsStep.svelte";
  import Closed from "./Closed.svelte";
  import DoneStep from "./DoneStep.svelte";
  import FamilyStep from "./FamilyStep.svelte";
  import ImmichStep from "./ImmichStep.svelte";
  import SignInStep from "./SignInStep.svelte";
  import Welcome from "./Welcome.svelte";
  import {
    arrivedFromTest, browser, BUTTON, failed, firstStep, linked, markOf, rememberedMarks, rememberedStep, rememberMarks, rememberStep, ROW, STEPS, stepLine,
    WIDE_STEPS, type Counts, type Mark, type Step,
  } from "./wizard";

  type Screen =
    | { kind: "loading" }
    | { kind: "load-failed"; message: string }
    | { kind: "welcome" }
    | { kind: "closed" }
    | { kind: "elsewhere" }
    | { kind: "step"; step: Step };

  let screen = $state<Screen>({ kind: "loading" });
  let setup = $state<SetupState | null>(null);
  let family = $state<Family | null>(null);
  let familyError = $state("");
  let marks = $state(rememberedMarks());
  let works = $state<string | null>(null);
  let busy = $state(false);
  let startError = $state("");
  let main: HTMLElement | undefined = $state();

  const counts = $derived<Counts | null>(family && !familyError ? { people: family.people.length, accounts: family.accounts.length, pixels: family.pixels.length } : null);
  const current = $derived(screen.kind === "step" ? screen.step : null);
  const wide = $derived(current !== null && WIDE_STEPS.includes(current));
  const showSteps = $derived(screen.kind === "welcome" || screen.kind === "step");

  const unauthorized = (err: unknown): boolean => err instanceof ApiError && err.status === 401;

  onMount(() => {
    const ended = () => { if (screen.kind === "step") screen = { kind: "closed" }; };
    window.addEventListener(SIGNED_OUT, ended);
    void load();
    return () => window.removeEventListener(SIGNED_OUT, ended);
  });

  async function show(next: Screen): Promise<void> {
    screen = next;
    await tick();
    main?.querySelector("h1")?.focus();
  }

  async function load(): Promise<void> {
    screen = { kind: "loading" };
    let state: SetupState;
    try {
      state = await apiCall<"GET /api/setup">("/api/setup");
    } catch (err) {
      screen = unauthorized(err) ? { kind: "welcome" } : { kind: "load-failed", message: failed("load setup", errMsg(err)) };
      return;
    }
    await enter(state, false);
  }

  async function enter(state: SetupState, focus: boolean): Promise<void> {
    setup = state;
    if (state.phase === "after_test") {
      try {
        family = await loadFamily();
      } catch (err) {
        screen = unauthorized(err) ? { kind: "closed" } : { kind: "load-failed", message: failed("load setup", errMsg(err)) };
        return;
      }
      if (arrivedFromTest()) works = state.tested_email ? `Sign-in works. You're signed in as ${state.tested_email}.` : "Sign-in works.";
    }
    const step = firstStep(state, counts, rememberedStep());
    rememberStep(step);
    if (focus) await show({ kind: "step", step });
    else screen = { kind: "step", step };
  }

  async function start(): Promise<void> {
    busy = true;
    startError = "";
    try {
      const state = await apiCall<"POST /api/setup/claim">("/api/setup/claim", { method: "POST" });
      await enter(state, true);
    } catch (err) {
      if (unauthorized(err)) await show({ kind: "closed" });
      else if (err instanceof ApiError && err.status === 409) await show({ kind: "elsewhere" });
      else startError = failed("start setup", errMsg(err));
    } finally {
      busy = false;
    }
  }

  function go(step: Step): void {
    works = null;
    rememberStep(step);
    void show({ kind: "step", step });
  }

  function mark(step: Step, value: Mark): void {
    marks = { ...marks, [step]: value };
    rememberMarks(marks);
  }

  function saved(next: SetupState, step: Step): void {
    setup = next;
    go(step);
  }

  async function reloadState(): Promise<void> {
    try {
      setup = await apiCall<"GET /api/setup">("/api/setup");
    } catch (err) {
      if (unauthorized(err)) screen = { kind: "closed" };
    }
  }

  async function reloadFamily(): Promise<void> {
    try {
      family = await loadFamily();
      familyError = "";
    } catch (err) {
      if (unauthorized(err)) screen = { kind: "closed" };
      else familyError = errMsg(err);
    }
  }

  function finished(): void {
    rememberMarks({});
    browser.leave("/");
  }
</script>

<div class="grid min-h-dvh content-start md:content-normal md:grid-cols-[19rem_minmax(0,1fr)]">
  <div class="flex flex-col gap-4 px-4 pt-6 md:gap-12 md:bg-rail md:px-10 md:py-16">
    <div class="flex items-center gap-3 text-lg font-bold">
      <span class="block size-3.5 rounded-full bg-accent"></span>Darkroom
    </div>
    {#if current}
      <p class="text-ink-muted md:hidden">{stepLine(current)}</p>
    {/if}
    {#if showSteps}
      <nav class="hidden md:block" aria-label="Setup">
        <ol class="flex list-decimal flex-col gap-1.5 pl-6 text-md text-ink-muted" aria-label="Setup steps">
          {#each STEPS as item (item.key)}
            {@const itemMark = setup && current !== item.key ? markOf(item.key, setup, counts, marks) : null}
            <li class={["min-h-8", current === item.key ? "font-bold text-ink" : itemMark && "text-ink"]} aria-current={current === item.key ? "step" : undefined}>
              {#if setup && linked(item.key, setup, itemMark)}
                <a class="underline" href="/setup" onclick={(e) => { e.preventDefault(); go(item.key); }}>{item.name}</a>
              {:else}
                {item.name}
              {/if}
              {#if itemMark}<span class="ml-2 text-base text-ink-muted">{itemMark === "done" ? "Done" : "Skipped"}</span>{/if}
            </li>
          {/each}
        </ol>
      </nav>
    {/if}
  </div>
  <main bind:this={main} class={["px-4 pt-4 pb-8 md:py-16", wide ? "md:px-10" : "md:px-12"]}>
    <section class="card w-full gap-4 p-5 md:gap-5 md:p-8">
      {#if screen.kind === "loading"}
        <p role="status" class="text-ink-muted">Loading...</p>
      {:else if screen.kind === "load-failed"}
        <FormError message={screen.message} />
        <div class={ROW}>
          <button type="button" class={BUTTON} onclick={() => void load()}>Try again</button>
        </div>
      {:else if screen.kind === "welcome"}
        <Welcome {busy} error={startError} onstart={start} />
      {:else if screen.kind === "closed"}
        <Closed elsewhere={false} />
      {:else if screen.kind === "elsewhere"}
        <Closed elsewhere {busy} error={startError} onretry={start} />
      {:else if setup}
        {#key screen.step}
          {#if screen.step === "address"}
            <AddressStep {setup} onsaved={(next) => saved(next, "sign-in")} onback={() => void show({ kind: "welcome" })} reload={reloadState} />
          {:else if screen.step === "sign-in"}
            <SignInStep {setup} onstate={(next) => (setup = next)} onback={() => go("address")} goAddress={() => go("address")} reload={reloadState} />
          {:else if screen.step === "immich"}
            <ImmichStep {setup} {works} onsaved={(next) => saved(next, "people")} reload={reloadState} />
          {:else if screen.step === "alerts"}
            <AlertsStep {setup} onsaved={(next) => { mark("alerts", "done"); saved(next, "done"); }} onskip={() => { mark("alerts", "skipped"); go("done"); }} onback={() => go("pixels")} reload={reloadState} />
          {:else if screen.step === "done" && family}
            <DoneStep {setup} {counts} failed={familyError} retry={reloadFamily} onfinished={finished} onback={() => go("alerts")} />
          {:else if family}
            {@const part = screen.step === "people" ? "people" : screen.step === "accounts" ? "accounts" : "pixels"}
            <FamilyStep
              {part}
              data={family}
              failed={familyError}
              retry={reloadFamily}
              onchanged={() => void reloadFamily()}
              oncontinue={() => go(part === "people" ? "accounts" : part === "accounts" ? "pixels" : "alerts")}
              onskip={() => { mark(part, "skipped"); go(part === "accounts" ? "pixels" : "alerts"); }}
              onback={() => go(part === "people" ? "immich" : part === "accounts" ? "people" : "accounts")}
            />
          {/if}
        {/key}
      {/if}
    </section>
  </main>
</div>
