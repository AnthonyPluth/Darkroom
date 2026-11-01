import { ApiError, SIGNED_OUT } from "./api";
import { errMsg } from "./act";
import type { Me } from "./api-types";
import { apiCall } from "./contract";

export type SessionState =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "signed-in"; me: Me }
  | { kind: "unreachable"; message: string };

class Session {
  current = $state<SessionState>({ kind: "loading" });

  async load(): Promise<void> {
    this.current = { kind: "loading" };
    try {
      this.current = { kind: "signed-in", me: await apiCall<"GET /api/me">("/api/me") };
    } catch (err) {
      this.current = err instanceof ApiError && err.status === 401 ? { kind: "signed-out" } : { kind: "unreachable", message: errMsg(err) };
    }
  }

  signedOut(): void {
    this.current = { kind: "signed-out" };
  }
}

export const session = new Session();

export function watchSignOut(): () => void {
  const leave = () => session.signedOut();
  window.addEventListener(SIGNED_OUT, leave);
  return () => window.removeEventListener(SIGNED_OUT, leave);
}

export function displayName(me: Me): string {
  return me.name || me.email || "Signed in";
}
