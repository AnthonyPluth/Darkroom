# tools/control: command reference

Generated from the CLI's own help by `make verify-skill` (`node frontend/verify/control.mjs help --markdown`); `make fleet-checks` fails when it is out of date. Don't edit it by hand.

```text
tools/control: drive the real app on demo data, one command at a time, and prove what you changed.

Usage: tools/control <command> [arguments] [options]

Start with `doctor`, then `start`; finish with `stop`. Output is JSON ({"ok": true, …}, or {"ok": false, "error": {code, message, hint}});
--text prints it for people. Exit 0 on success, 1 when the command failed, 2 for a usage error. `<command> --help` says more.

Session:
  doctor       Check this machine can run a session, and say how to fix what can't.
  start        Start a session: a demo server on fresh demo data under the frozen clock, and a browser on it.
  stop         Stop the session: its browser, its demo server and its copy of the data.
  cleanup      Remove what a crashed session left: its state file, data folders and processes.
  reset        Fresh demo data: a new copy of the seed on a new demo server and a new browser, and the page opened again.

Navigate:
  goto         Open a page of the app.
  viewport     Resize the browser to one of make verify's sizes: phone 440×956, desktop 1280×800, laptop 1024×768, wide 1440×900.

Inspect:
  info         Where the browser is: route, title, viewport, the frozen clock and the session.
  snapshot     The page's accessibility tree as text: roles and the names to click.
  screenshot   Save a screenshot once the page has settled, and print its path.
  wait-settle  Wait until the page has settled, by make verify's rule, and say what is still loading if not.

Interact:
  click        Click a target (a role and name, a selector or text).
  type         Fill a field with text, or pick a select's option (the last argument is the text).
  press        Press a key, or a chord, in the focused element.
  scroll       Scroll the page: up, down, top, bottom, or until a target is in view.
  eval         Evaluate JavaScript in the page and print the result (read-only unless --write).

Observe:
  console      The console messages and page errors since the last call.
  network      The requests since the last call: method, path, status, and the ones that failed.
  fake-immich  What the fake Immich recorded while a --with-import session was seeded.

Flows:
  flows        List make verify's flows (frontend/verify/flows/), with the page and widths each starts on.
  flow         Run one verify flow on the session, step by step, and report each step.

Help:
  help         This overview, or one command's help (`help <command>`, the same as `<command> --help`).

Localhost and demo data only: it drives only the demo server it started (a request to any other address is blocked before it is sent), and writes only under artifacts/control/ (never committed). Screenshots for a pull request come from `make verify`, never from here.
```

## Session

### doctor

```text
tools/control doctor

Check this machine can run a session, and say how to fix what can't.

Checks Node, the web app's packages and build, Chromium, Python with Darkroom's packages, libfaketime (the frozen clock), a free loopback port,
the machine's load, a running make verify, this checkout's session (none, running or stale) and processes a crashed session left.
Each check has ok, detail and, when it fails, fix: the command to run. Exits 1 when a check fails.

Examples:
  tools/control doctor --text
```

### start

```text
tools/control start [options]

Start a session: a demo server on fresh demo data under the frozen clock, and a browser on it.

The demo data is `run.py demo`'s. The browser is signed in as the demo member.
--with-import adds what make verify seeds on top: a second export built from the demo originals, imported through `run.py import-takeout` against a fake Immich on 127.0.0.1 (`fake-immich scans` lists the rescans it recorded). Only with it do pages look as in docs/screenshots/.
The server and the seed run under libfaketime at make verify's frozen moment, and the browser's clock is fixed at the same moment (UTC).
One session per checkout, on a port and data of its own: a running make verify is left alone.
Refuses when the machine is busy (more than 1.5 runnable processes per CPU) unless --anyway. A stale session (its process gone) is cleaned up first.

Options:
  --viewport <phone|desktop|laptop|wide>  the browser's size to start at (default phone)
  --idle-minutes <number>  stop by itself after this long with no command (default 30)
  --anyway  start even though the machine is busy (pages may load slowly)
  --with-import  also import a made-up Takeout export against a fake Immich, so Imports and the review queue hold a real run

Examples:
  tools/control start
  tools/control start --viewport desktop
  tools/control start --with-import
```

### stop

```text
tools/control stop [options]

Stop the session: its browser, its demo server and its copy of the data.

Screenshots under artifacts/control/ stay (cleanup --shots removes them). Stopping when there is no session is not an error.

Options:
  --dry-run  say what it would do, and do nothing

Examples:
  tools/control stop
  tools/control stop --dry-run
```

### cleanup

```text
tools/control cleanup [options]

Remove what a crashed session left: its state file, data folders and processes.

Only ever this checkout's control files and processes (found by artifacts/control/ in their command line or data folder).
A running session is left alone: stop it first.

Options:
  --dry-run  say what it would do, and do nothing
  --shots  also delete the screenshots under artifacts/control/

Examples:
  tools/control cleanup --dry-run
  tools/control cleanup --shots
```

### reset

```text
tools/control reset [options]

Fresh demo data: a new copy of the seed on a new demo server and a new browser, and the page opened again.

What you changed (added a person, revoked an iPhone) is gone. The viewport and route stay; the port changes, and the browser is new and signed in again.

Options:
  --dry-run  say what it would do, and do nothing

Examples:
  tools/control reset --dry-run
  tools/control reset
```

## Navigate

### goto

```text
tools/control goto <route> [options]

Open a page of the app.

A route is a page name (`status`, `people`, `exports`) or a path (`/people`); a full URL only on the session's own server (anything else is refused).
`sign-in` signs the browser out and opens the sign-in page; any other route signs it in again as the demo member.
Waits for the page to settle (as wait-settle) and says whether it did. After changing data, a page can show what the app had loaded before: --reload shows the server's.

Options:
  --reload  then reload the page: it fetches what the app kept in memory again

Examples:
  tools/control goto people
  tools/control goto /exports
  tools/control goto sign-in
  tools/control goto status --reload
```

### viewport

```text
tools/control viewport <phone|desktop|laptop|wide>

Resize the browser to one of make verify's sizes: phone 440×956, desktop 1280×800, laptop 1024×768, wide 1440×900.

Only phone and desktop are sizes make verify takes images at; laptop and wide are for the flows that list them.

Examples:
  tools/control viewport desktop
```

## Inspect

### info

```text
tools/control info

Where the browser is: route, title, viewport, the frozen clock and the session.

Examples:
  tools/control info --text
```

### snapshot

```text
tools/control snapshot [options]

The page's accessibility tree as text: roles and the names to click.

Each line is `- role "name"`, indented by nesting: the role and name are what click, type and the role target take.

Options:
  --root <string>  only this part of the page (a selector), e.g. [role=dialog]
  --max-lines <number>  cut the tree after this many lines (it says so) (default 300)

Examples:
  tools/control snapshot --text
  tools/control snapshot --root '[role=dialog]' --text
```

### screenshot

```text
tools/control screenshot [path] [options]

Save a screenshot once the page has settled, and print its path.

Taken as make verify takes one: the network idle, fonts loaded, animations finished, a short settle, then the image with CSS animations disabled.
The default is the whole page, scrolled to the top first; --viewport is only the window, where it is scrolled to now (scroll first to capture another part).
The path must be under artifacts/ (default artifacts/control/shots/<n>-<route>-<viewport>.png). Look at it: it is the evidence for you.
Never copy one into docs/screenshots/ or a pull request: the images a pull request carries are what make verify writes there.

Options:
  --full  the whole page (the default)
  --viewport  only the window, where it's scrolled to

Examples:
  tools/control screenshot
  tools/control screenshot --viewport
  tools/control screenshot artifacts/control/before.png
```

### wait-settle

```text
tools/control wait-settle

Wait until the page has settled, by make verify's rule, and say what is still loading if not.

Settled: the network idle for 500 ms, fonts loaded and no animation still running (each waits up to 3 seconds), a short settle, and no Loading indicator showing (up to 5 seconds).
goto, click, type and the other commands that change the page report the same.

Examples:
  tools/control wait-settle
```

## Interact

### click

```text
tools/control click <target…> [options]

Click a target (a role and name, a selector or text).

A target is one of: a role and its accessible name (`button Save`, `link People`, `textbox Name`: the names `snapshot` shows),
a selector (`#id`, `.class`, `[data-testid=x]`, `css=…`, `text=…`, `role=…`, `button:has-text('Save')`), or else visible text (`Add person`).
Several matches are an error that lists them: pick one with --nth, or name it more exactly.

Options:
  --nth <number>  which of several matches to use (0 is the first); without it, more than one match is an error
  --exact  match the name or text exactly (case-sensitive), not as a substring
  --timeout <number>  how long to wait for the target, in ms (default 10000)

Examples:
  tools/control click button Save
  tools/control click link People --nth 1
  tools/control click 'a[href="/people"]' --nth 0
```

### type

```text
tools/control type <target…> <text> [options]

Fill a field with text, or pick a select's option (the last argument is the text).

A target is one of: a role and its accessible name (`button Save`, `link People`, `textbox Name`: the names `snapshot` shows),
a selector (`#id`, `.class`, `[data-testid=x]`, `css=…`, `text=…`, `role=…`, `button:has-text('Save')`), or else visible text (`Add person`).
Several matches are an error that lists them: pick one with --nth, or name it more exactly.
The field's current value is replaced. On a select (a combobox with options) it picks the option with that label.
Text that starts with - goes after --: `type textbox Amount -- -5`.

Options:
  --nth <number>  which of several matches to use (0 is the first); without it, more than one match is an error
  --exact  match the name or text exactly (case-sensitive), not as a substring
  --timeout <number>  how long to wait for the target, in ms (default 10000)

Examples:
  tools/control type textbox Name 'Sam Example'
  tools/control type '#email' 'sam@example.com'
  tools/control type combobox Default Avery
```

### press

```text
tools/control press <key>

Press a key, or a chord, in the focused element.

Keys are Playwright's names: Enter, Escape, Tab, Shift+Tab, ArrowDown, Control+a.

Examples:
  tools/control press Escape
  tools/control press Shift+Tab
```

### scroll

```text
tools/control scroll [where…] [options]

Scroll the page: up, down, top, bottom, or until a target is in view.

Without an argument it scrolls down a screen.

Options:
  --nth <number>  which of several matches to use (0 is the first); without it, more than one match is an error
  --exact  match the name or text exactly (case-sensitive), not as a substring

Examples:
  tools/control scroll
  tools/control scroll bottom
  tools/control scroll heading Pixels
```

### eval

```text
tools/control eval <js…> [options]

Evaluate JavaScript in the page and print the result (read-only unless --write).

An expression (`document.title`) or a function (`() => [...document.querySelectorAll('h2')].map(h => h.textContent)`); the result as JSON.
Read-only by default: a request other than GET or HEAD is blocked, one the expression schedules for later too (the session blocks every change the page tries to make unless a command that acts as a person is running: goto, click, type, press, scroll, viewport, flow, reset, or eval --write). A change it makes to the page is reported (domChanges).
Prefer snapshot, click and type: they act as a person would.

Options:
  --write  allow it to change the app's data (requests other than GET)

Examples:
  tools/control eval document.title
  tools/control eval "() => getComputedStyle(document.body).backgroundColor"
```

## Observe

### console

```text
tools/control console

The console messages and page errors since the last call.

make verify fails a page on a console error or an uncaught page error: so should you.

Examples:
  tools/control console --text
```

### network

```text
tools/control network

The requests since the last call: method, path, status, and the ones that failed.

Paths only: no query strings, bodies or ids (numbers become :id). make verify fails a page on a 5xx.

Examples:
  tools/control network --text
```

### fake-immich

```text
tools/control fake-immich <scans>

What the fake Immich recorded while a --with-import session was seeded.

`scans` lists the library rescans the import asked the fake for: one per library whose files changed, with the person it belongs to.
Only a session started with `start --with-import` has them; the fake itself stops once the import is done, and nothing leaves 127.0.0.1.

Examples:
  tools/control fake-immich scans --text
```

## Flows

### flows

```text
tools/control flows

List make verify's flows (frontend/verify/flows/), with the page and widths each starts on.

Examples:
  tools/control flows --text
```

### flow

```text
tools/control flow <name> [options]

Run one verify flow on the session, step by step, and report each step.

Its screenshots go to artifacts/control/flows/, never artifacts/verify/ or docs/screenshots/, and are taken at every size (make verify writes images at phone and desktop only). It stops at the first failed step, with a -FAILED- image.
A flow starts from the demo data as make verify seeds it: run `reset` first if you changed anything.
It runs in a new browser, as make verify's flows do, at the session's size (or --viewport).
Afterwards the session is that browser, as the flow left it, so you can look around: `reset` gives a fresh one. After a screenshot, the flow's expect_text steps since its last action must still hold.

Options:
  --viewport <phone|desktop|laptop|wide>  resize to this first (default: the session's)

Examples:
  tools/control flow open-people
  tools/control flow open-people --viewport desktop
```

## Help

### help

```text
tools/control help [command] [options]

This overview, or one command's help (`help <command>`, the same as `<command> --help`).

Options:
  --markdown  the whole reference as Markdown (references/cli.md)

Examples:
  tools/control help click
```
