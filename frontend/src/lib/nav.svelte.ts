export type Page = { path: string; label: string };

export const PAGES: readonly Page[] = [
  { path: "/", label: "Status" },
  { path: "/people", label: "People" },
  { path: "/exports", label: "Exports" },
  { path: "/imports", label: "Imports" },
  { path: "/review", label: "Review" },
  { path: "/browser", label: "Browser" },
];

export function pageFor(path: string): Page | null {
  const trimmed = path.length > 1 ? path.replace(/\/+$/, "") : path;
  return PAGES.find((page) => page.path === trimmed) ?? null;
}

class Router {
  path = $state(location.pathname);

  go(path: string): void {
    if (path === this.path) return;
    history.pushState(null, "", path);
    this.path = path;
  }

  sync(): void {
    this.path = location.pathname;
  }
}

export const router = new Router();

export function watchHistory(): () => void {
  const sync = () => router.sync();
  window.addEventListener("popstate", sync);
  return () => window.removeEventListener("popstate", sync);
}

export function followLink(event: MouseEvent, path: string): void {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  router.go(path);
}
