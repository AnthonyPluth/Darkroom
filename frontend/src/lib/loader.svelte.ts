import { actGet } from "./act";

export type Loaded<T> = { kind: "loading" } | { kind: "ready"; data: T } | { kind: "error"; message: string };

export class Loader<T> {
  current = $state.raw<Loaded<T>>({ kind: "loading" });

  private started = 0;
  private settled = 0;
  private barrier = 0;

  constructor(private readonly fetchAll: () => Promise<T>, private readonly applied: (data: T) => void = () => {}) {}

  load(quiet = false): Promise<void> {
    return this.run(quiet, true);
  }

  poll(): Promise<void> {
    return this.run(true, false);
  }

  private async run(quiet: boolean, explicit: boolean): Promise<void> {
    const order = ++this.started;
    if (explicit) this.barrier = order;
    if (!quiet) this.current = { kind: "loading" };
    let message = "";
    const data = await actGet(this.fetchAll, { onError: (m) => (message = m) });
    if (order < this.settled || (!explicit && order < this.barrier)) return;
    this.settled = order;
    if (data !== null) this.applied(data);
    this.current = data === null ? { kind: "error", message } : { kind: "ready", data };
  }
}
