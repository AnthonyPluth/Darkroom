import { apiCall } from "./contract";
import { ignoreFailure } from "./act";

const PAST_THE_LAST_PAIR = 2_147_483_647;

class Pending {
  count = $state(0);
  generation = 0;
}

export const pending = new Pending();

export function showPending(count: number): void {
  pending.generation += 1;
  pending.count = count;
}

export async function refreshPending(): Promise<void> {
  const generation = ++pending.generation;
  let count = 0;
  try {
    count = (await apiCall<"GET /api/review">(`/api/review?status=pending&after=${PAST_THE_LAST_PAIR}`)).pending;
  } catch {
    ignoreFailure();
  }
  if (generation === pending.generation) {
    pending.count = count;
  }
}
