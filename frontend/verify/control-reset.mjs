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
  await commit(fresh);
}

export async function setUpContext(browser, options, setUp) {
  const fresh = await browser.newContext(options);
  try {
    await setUp(fresh);
  } catch (e) {
    await fresh.close().catch(() => null);
    throw e;
  }
  return fresh;
}

export async function rollbackSwap({ prior, current, save }) {
  if (current.ctx !== prior.ctx) await current.ctx.close().catch(() => null);
  save(prior);
  await current.fresh.server.stop();
  return prior;
}

export async function commitSwap({ prior, fresh, save, log = () => {} }) {
  save(fresh);
  await prior.ctx.close().catch(() => null);
  await prior.server.stop().catch((e) => log(String(e?.message ?? e).split("\n")[0]));
}
