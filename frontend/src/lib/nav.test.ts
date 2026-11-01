// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { followLink, pageFor, PAGES, router, watchHistory } from "./nav.svelte";

afterEach(() => { history.replaceState(null, "", "/"); router.sync(); });

const click = (init: MouseEventInit = {}) => new MouseEvent("click", { button: 0, cancelable: true, ...init });

describe("pages", () => {
  it("are the shell's six, Status first", () => {
    expect(PAGES.map((p) => p.label)).toEqual(["Status", "People", "Exports", "Imports", "Review", "Browser"]);
  });
  it("are found by path, with or without a trailing slash", () => {
    expect(pageFor("/")?.label).toBe("Status");
    expect(pageFor("/people/")?.label).toBe("People");
    expect(pageFor("/nowhere")).toBeNull();
  });
});

describe("the router", () => {
  it("follows a plain click without reloading the page", () => {
    const event = click();
    followLink(event, "/exports");
    expect(event.defaultPrevented).toBe(true);
    expect(router.path).toBe("/exports");
    expect(location.pathname).toBe("/exports");
  });

  it("leaves a modified or already handled click to the browser", () => {
    for (const init of [{ metaKey: true }, { ctrlKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]) {
      const event = click(init);
      followLink(event, "/review");
      expect(event.defaultPrevented).toBe(false);
    }
    const handled = click();
    handled.preventDefault();
    followLink(handled, "/review");
    expect(router.path).toBe("/");
  });

  it("doesn't push the page it is already on", () => {
    const before = history.length;
    router.go("/");
    expect(history.length).toBe(before);
  });

  it("follows the back button", () => {
    const stop = watchHistory();
    history.pushState(null, "", "/imports");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(router.path).toBe("/imports");
    stop();
    history.pushState(null, "", "/browser");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(router.path).toBe("/imports");
  });
});
