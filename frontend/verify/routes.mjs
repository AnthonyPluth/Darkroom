export const ROUTES = { "sign-in": "/", status: "/", people: "/people", exports: "/exports", imports: "/imports", review: "/review", browser: "/browser" };
export const SIGNED_OUT_PAGES = ["sign-in"];
export const routeOf = (name) => ROUTES[name] ?? `/${name}`;

export const LOOPBACK_HOSTS = ["127.0.0.1", "localhost"];

export function sameServer(url, base) {
  const own = new URL(base);
  return url.protocol === own.protocol && url.port === own.port && LOOPBACK_HOSTS.includes(url.hostname);
}

export function allowedRequest(url, base) {
  let u;
  try { u = new URL(url); } catch { return false; }
  return ["data:", "blob:", "about:"].includes(u.protocol) || sameServer(u, base);
}
