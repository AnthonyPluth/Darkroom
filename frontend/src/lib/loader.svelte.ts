import { actGet } from "./act";

export type Loaded<T> = { kind: "loading" } | { kind: "ready"; data: T } | { kind: "error"; message: string };

export class Loader<T> {
  current = $state.raw<Loaded<T>>({ kind: "loading" });

  private generation = 0;

  constructor(private readonly fetchAll: () => Promise<T>, private readonly applied: (data: T) => void = () => {}) {}

  async load(quiet = false): Promise<void> {
    const generation = ++this.generation;
    if (!quiet) this.current = { kind: "loading" };
    let message = "";
    const data = await actGet(this.fetchAll, { onError: (m) => (message = m) });
    if (generation !== this.generation) return;
    if (data !== null) this.applied(data);
    this.current = data === null ? { kind: "error", message } : { kind: "ready", data };
  }
}
