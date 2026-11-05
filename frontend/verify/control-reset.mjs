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
