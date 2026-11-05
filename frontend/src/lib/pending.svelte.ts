import { apiCall } from "./contract";
import { ignoreFailure } from "./act";

const PAST_THE_LAST_PAIR = 2_147_483_647;

class Pending {
  count = $state(0);
  generation = 0;
}

export const pending = new Pending();

export function reservePending(): number {
  return ++pending.generation;
}

export function showPending(generation: number, count: number): void {
  if (generation === pending.generation) {
    pending.count = count;
  }
}

export async function refreshPending(): Promise<void> {
  const generation = reservePending();
  let count = 0;
  try {
    count = (await apiCall<"GET /api/review">(`/api/review?status=pending&after=${PAST_THE_LAST_PAIR}`)).pending;
  } catch {
    ignoreFailure();
  }
  showPending(generation, count);
}
