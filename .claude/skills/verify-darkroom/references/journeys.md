# Journeys

End-to-end recipes. Each assumes `tools/control doctor` passed. Run them from the repository root and look at every screenshot you take.

## 1. Reproduce a UI bug on People

```sh
tools/control start --viewport desktop
tools/control goto people
tools/control snapshot --root main --text --max-lines 60          # the names to target
tools/control click button "Edit Avery Example"                    # a person's form
tools/control wait-settle
tools/control snapshot --root '[role=dialog]' --text
tools/control screenshot
tools/control console --text                                       # any error is a bug
tools/control network --text                                       # any 5xx is a bug
```

Write down the steps that show the bug and the command output that proves it. Then turn it into a failing test, or into a flow in `frontend/verify/flows/`. `reset` gets you fresh data for another attempt.

## 2. Check a layout at both widths

```sh
tools/control start
tools/control goto people
tools/control screenshot                           # phone, 440×956, the whole page
tools/control screenshot --viewport                # phone, the window as it is scrolled now (the first screen, here)
tools/control viewport desktop
tools/control screenshot                           # desktop, 1280×800
```

Open all three images. A phone layout that overflows, a clipped label or a fixed bar over content shows up there. `viewport laptop` and `wide` show the other sizes the flows use; for anything between, resize a real browser by hand.

## 3. Look at the signed-out page

```sh
tools/control start
tools/control goto sign-in
tools/control screenshot
tools/control goto status                          # signed in again as the demo member
```

## 4. Compare before and after a change to the web app

```sh
tools/control start
tools/control goto people
tools/control screenshot artifacts/control/before-people-phone.png
# ...edit frontend/src...
(cd frontend && npm run build)
tools/control reset                                # a new server serves the new build, on fresh data
tools/control screenshot artifacts/control/after-people-phone.png
cmp artifacts/control/before-people-phone.png artifacts/control/after-people-phone.png || echo changed
```

Look at both images. They are for you; the pull request's images come from `make verify`.

## 5. Get the images a pull request carries

```sh
make verify                                        # or: make verify PAGES="people"
git status docs/screenshots                        # the images that changed with your change
```

`make verify` writes `docs/screenshots/<page>-phone.png` and `-desktop.png` only when the run reports OK (a failed run leaves them untouched and keeps what it took in `artifacts/verify/images/`), and its report to `artifacts/verify/`. A change meant to alter a page shows as a changed image; one that isn't must show none. Commit them with the change.

## 6. Run one of make verify's flows step by step

```sh
tools/control flows --text                         # names, start pages, widths
tools/control flow open-people --text              # each step ok or FAIL, with the time it took
tools/control flow sign-out --viewport desktop
```

A failed step stops the flow, saves `artifacts/control/flows/flow-<name>-FAILED-<width>.png`, and exits 1; the answer still has every step's result. Afterwards the session is the flow's browser as it ended, so `snapshot` and `screenshot` show where it stopped. Run `reset` before a second run.
