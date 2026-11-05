export class ReplaceFailed extends Error {
  constructor(stage, cause) {
    super(String(cause?.message ?? cause).split("\n")[0], { cause });
    this.stage = stage;
  }
}

export async function replaceServer({ launch, open, rollback, commit }) {
  let fresh;
  try { fresh = await launch(); } catch (e) { throw new ReplaceFailed("launch", e); }
  try { await open(fresh); } catch (e) {
    await rollback(fresh).catch(() => null);
    throw new ReplaceFailed("open", e);
  }
  try { await commit(fresh); } catch (e) {
    await rollback(fresh).catch(() => null);
    throw new ReplaceFailed("commit", e);
  }
}

export async function setUpContext(browser, options, setUp) {
  const context = await browser.newContext(options);
  try {
    return { context, value: await setUp(context) };
  } catch (e) {
    await context.close().catch(() => null);
    throw e;
  }
}

export async function rollbackSwap({ prior, current, save }) {
  if (current.ctx !== prior.ctx) await current.ctx.close().catch(() => null);
  await Promise.resolve().then(() => save(prior)).catch(() => null);
  await Promise.resolve().then(() => current.fresh.server.stop()).catch(() => null);
  return prior;
}

export async function commitSwap({ prior, fresh, save, log = () => {} }) {
  save(fresh);
  await prior.ctx.close().catch(() => null);
  await prior.server.stop().catch((e) => log(String(e?.message ?? e).split("\n")[0]));
}
