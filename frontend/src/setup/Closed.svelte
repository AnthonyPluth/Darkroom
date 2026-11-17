<script lang="ts">
  import FormError from "../forms/FormError.svelte";
  import { BUTTON, PRIMARY, ROW } from "./wizard";

  let { elsewhere, busy = false, error = "", onretry }: { elsewhere: boolean; busy?: boolean; error?: string; onretry?: () => void } = $props();
</script>

{#if elsewhere}
  <h1 tabindex="-1" class="text-xl font-bold tracking-tight">Setup is open in another browser</h1>
  <p>Another browser started Darkroom's setup first, and only that browser can continue it. Finish it there.</p>
  <p class="text-ink-muted">If that browser is lost, run <code class="font-mono text-sm">python run.py reopen-setup</code> in the Darkroom container's console, then press Try again. After the test sign-in, that closes setup instead; sign in and carry on from the People page.</p>
  <div class={ROW}>
    <button type="button" class={PRIMARY} disabled={busy} onclick={onretry}>Try again</button>
  </div>
  <FormError message={error} />
{:else}
  <h1 tabindex="-1" class="text-xl font-bold tracking-tight">Setup isn't open</h1>
  <p>Darkroom's first-run setup opens only when all of these hold:</p>
  <ul class="flex list-disc flex-col gap-1.5 pl-5">
    <li><code class="font-mono text-sm">DARKROOM_WIZARD=1</code> is set in the container's settings.</li>
    <li>Setup hasn't been finished, and sign-in isn't set up in the environment.</li>
    <li>This device is on your home network.</li>
    <li>You opened <code class="font-mono text-sm break-all">http://&lt;server's address&gt;:8000/setup</code> directly, not through Traefik or another proxy.</li>
    <li>After the test sign-in: you're still signed in, on the browser that started setup.</li>
  </ul>
  <p class="text-ink-muted">If setup was closed before you finished, sign in and carry on from the People page. Settings are changed in Darkroom's environment.</p>
  <div class={ROW}>
    <a class={BUTTON} href="/">Go to sign-in</a>
  </div>
{/if}
