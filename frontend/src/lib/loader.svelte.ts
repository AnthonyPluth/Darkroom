import { actGet } from "./act";

export type Loaded<T> = { kind: "loading" } | { kind: "ready"; data: T } | { kind: "error"; message: string };

export class Loader<T> {
  current = $state.raw<Loaded<T>>({ kind: "loading" });

  constructor(private readonly fetchAll: () => Promise<T>) {}

  async load(quiet = false): Promise<void> {
    if (!quiet) this.current = { kind: "loading" };
    let message = "";
    const data = await actGet(this.fetchAll, { onError: (m) => (message = m) });
    this.current = data === null ? { kind: "error", message } : { kind: "ready", data };
  }
}
