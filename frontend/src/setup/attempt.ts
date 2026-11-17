import { errMsg } from "$lib/act";
import { ApiError } from "$lib/api";
import { failed } from "./wizard";

export const IN_ENVIRONMENT = "is set in the environment";

export type Attempt = { what: string; busy: (on: boolean) => void; error: (message: string) => void; outdated?: () => void };

export async function attempt<T>(fn: () => Promise<T>, o: Attempt): Promise<T | null> {
  o.error("");
  o.busy(true);
  try {
    return await fn();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return null;
    o.error(failed(o.what, errMsg(err)));
    if (err instanceof ApiError && err.status === 409 && errMsg(err).includes(IN_ENVIRONMENT)) o.outdated?.();
    return null;
  } finally {
    o.busy(false);
  }
}
