import { apiCall } from "./contract";
import { ignoreFailure } from "./act";

const PAST_THE_LAST_PAIR = 2_147_483_647;

class Pending {
  count = $state(0);
}

export const pending = new Pending();

export async function refreshPending(): Promise<void> {
  try {
    pending.count = (await apiCall<"GET /api/review">(`/api/review?status=pending&after=${PAST_THE_LAST_PAIR}`)).pending;
  } catch {
    pending.count = 0;
    ignoreFailure();
  }
}
