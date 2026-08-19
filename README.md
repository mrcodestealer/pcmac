# watchgrafana

Keeps two Chrome windows pinned on the **Core Metrics Arms – Aliyun** dashboard.
One window watches the **top** of the dashboard, the other the **bottom**. When
either window goes blank/white, both windows are reloaded and each is scrolled
back to its assigned half.

Everything is driven through Chrome's own AppleScript interface, so it works on
whatever window you point it at — no clicking, no synthetic keystrokes, and it
keeps working while you are connected over AnyDesk.

## One-time setup

**1. Let Chrome run JavaScript from AppleScript** (one click, no restart):

> Chrome menu bar → **View** → **Developer** → **Allow JavaScript from Apple Events**

This is what lets the watchdog see inside the page and scroll it. Without it the
watchdog still runs but only detects crashed/error tabs, and cannot scroll.

*(Unattended alternative: quit Chrome, set `browser.allow_javascript_apple_events`
to `true` in `~/Library/Application Support/Google/Chrome/Local State`, relaunch.)*

**2. Check the setup:**

```bash
./watchgrafana.py doctor
```

Every line should say `[ok]`. It also prints which window got which role.

**3. Put it somewhere macOS does not protect.**

Do **not** leave this in `~/Downloads`, `~/Desktop` or `~/Documents`. Your terminal
can read those folders, but the launchd agent runs as a different process with no
access, so it dies with exit code 2 on every respawn without ever writing a log
line — `status` shows `spawn scheduled` and a climbing `runs` count. `~/watchgrafana`
is fine. `install` refuses to run from a protected folder, and `doctor` warns.

**4. Start it at login:**

```bash
./watchgrafana.py install
```

That loads a launchd agent (`me.junchen.watchgrafana`) which starts at login and
restarts the watchdog if it ever dies. `./watchgrafana.py uninstall` removes it.

## Commands

| command | what it does |
| --- | --- |
| `doctor` | check permissions, windows and role assignment — **start here** |
| `list` | list Chrome windows, mark the ones with the dashboard open |
| `pin` | re-pin roles; no args = auto-pin left-to-right, or `pin <topId> <bottomId>` |
| `probe` | one detailed health report per window (panels, scroll position, reasons) |
| `scroll <role> [target]` | apply a scroll target right now |
| `once [--dry-run]` | run a single check cycle; `--dry-run` reports without touching anything |
| `run` | watch forever (this is what launchd runs) |
| `test` | force the whole recovery path: reload both windows + re-scroll |
| `start` / `stop` | start or pause the background agent without uninstalling |
| `status` | is the agent running, and how many recoveries lately |
| `enable-js` | tick Chrome's "Allow JavaScript from Apple Events" (see note below) |
| `./selftest.py` | verify the injected JS and the health logic without touching the live windows |

Logs: `logs/watchgrafana.log` (rotated at 2 MB). `tail -f logs/watchgrafana.log`.

Once installed, the agent runs in the background and restarts at login — there is
nothing to keep open in a terminal, and no Ctrl-C involved. Do **not** also run
`watchgrafana run` by hand while the agent is installed: two watchdogs will fight
over reloading and scrolling the same windows. To watch what it is doing, tail the
log (Ctrl-C stops the tailing, not the watchdog). To pause it, `watchgrafana stop`.

## What counts as "white"

A window is unhealthy when any of these is true:

* the page has no text and no dashboard panels in the DOM (true blank page)
* panels exist but nothing rendered inside them
* Grafana bounced to `/login`, or drew its own error screen
* the tab is on a browser error page (`Aw, Snap!`, `chrome-error://`, ERR_*)
* the renderer stopped answering the health probe inside the timeout (hung tab)
* the tab navigated away from the dashboard URL
* *(optional)* the window is measurably white on screen — see below

Signals are graded. A **clearly** dead page - no text *and* no panels, a login
bounce, Grafana's error screen, `Aw, Snap!`, a `chrome-error://` tab - is acted on
the **first** time it is seen. Ambiguous ones - a half-rendered dashboard, a single
missed probe, a white-looking window - wait for `bad_checks_before_action` in a row,
so a slow refresh never triggers a reload.

Timing at the default 5s interval:

| | detected within |
| --- | --- |
| blank / crashed / login-bounced page | ~5s |
| half-rendered, hung renderer, white pixels | ~10s |

A cycle costs one `osascript` round-trip (~0.45s) because geometry, tab metadata and
the in-page probe for both windows are fetched together, and the full window/tab
enumeration only re-runs when a pinned tab stops matching.

## Recovery

1. If a monitor window is showing some other tab, its dashboard tab is brought
   back to the front.
2. **Both** windows reload.
3. Wait until panels are back in the DOM (up to `render_wait_seconds`).
4. Each window is scrolled to its own target — `top` stays at the top,
   `bottom` scrolls to the bottom, re-applying while Grafana lazily loads more
   panels and the page grows.

If the same window is still bad after twice the trip threshold, recovery
escalates: a hard re-navigation plus a 2-pixel window resize, which forces Chrome
to re-composite (this is the one that fixes DisplayLink-style white windows).

Recoveries are rate-limited: `cooldown_seconds` between attempts and
`max_recoveries_per_hour` overall, so a Grafana outage can never turn into a
reload loop.

## config.json

| key | default | meaning |
| --- | --- | --- |
| `url_contains` | `core-metrics-arms-aliyun` | how a monitoring tab is recognised |
| `role_order` | `["top","bottom"]` | leftmost matching window = `top`, next = `bottom` |
| `roles.<role>.window_id` | pinned ids | Chrome window id; re-pinned automatically after a Chrome restart |
| `roles.<role>.scroll` | `top` / `bottom` | `top`, `bottom`, `fraction:0.6`, `px:1200`, `row:Pulsar 投注` |
| `interval_seconds` | `5` | how often to check |
| `activate_tab_on_recovery` | `true` | pull the dashboard tab back to the front during recovery |
| `steady_scroll` | `on_reset` | `off`, `on_reset` (only fix a scroll that jumped back to the top), `always` |
| `bad_checks_before_action` | `2` | consecutive bad checks before reloading — **ambiguous signals only** |
| `render_poll_seconds` | `0.6` | how often to re-check while waiting for panels after a reload |
| `scroll_settle_delay` | `0.5` | pause between scroll re-applications as the page grows |
| `cooldown_seconds` | `90` | minimum gap between recoveries |
| `max_recoveries_per_hour` | `12` | hard cap |
| `render_wait_seconds` | `30` | how long to wait for panels after a reload |
| `pixel_check` | `off` | `off`, `auto`, `on` — see below |
| `white_frac_threshold` | `0.80` | fraction of near-white pixels that counts as blank |

`roles.<role>.scroll` takes a row name too, which is steadier than "bottom" if
you care about a specific section:

```json
"bottom": { "window_id": 1080876830, "scroll": "row:Pulsar 投注" }
```

`./watchgrafana.py probe` prints the row names it can see, so you can copy one.

## If Chrome will not allow JavaScript from Apple Events

`enable-js` drives the menu item through the accessibility API, which needs the
terminal to have Accessibility permission. On some machines Chrome ignores the
synthetic click entirely — the item reports `enabled=true`, the click reports
success, and no checkmark appears. When that happens, `enable-js-restart.py`
writes the pref (`browser.allow_javascript_apple_events`) straight into Chrome's
config instead. That needs Chrome restarted, so it saves every window's tabs and
bounds first and rebuilds them afterwards:

```bash
./enable-js-restart.py --dry-run              # show what it would do
./enable-js-restart.py --enable-session-restore
```

`--enable-session-restore` also switches Chrome to "Continue where you left off",
so the wall rebuilds itself after any future crash or reboot. If anything goes
wrong the layout is in `layout.json` and `--restore-only layout.json` replays it;
the original prefs are backed up beside the originals as `*.bak-<timestamp>`.

A Chrome update resets this setting. `doctor` reports it in one line when it happens.

## Optional: pixel-level white check

The DOM checks catch a blank page whatever the cause — *except* the rare case
where the page is fine but the GPU paints the window white (happens with
DisplayLink docks after display sleep). To catch that too, set
`"pixel_check": "auto"` and grant **Screen Recording** to whatever runs the
watchdog (System Settings → Privacy & Security → Screen Recording). It measures
only the two dashboard windows, skipping Chrome's toolbar.

It is `off` by default because Screen Recording is currently declined on this
Mac, and a window on a different Mission Control Space cannot be captured at all.

`bin/whitecheck` is the small helper that does this; it rebuilds itself from
`whitecheck.m` if the source is newer.

## Files

```
watchgrafana.py   the watchdog
config.json       settings (window ids, scroll targets, thresholds)
state.json        recovery history + bad-check streaks (written at runtime)
selftest.py       offline tests for the injected JS and the health logic
test-restore.py   offline tests for the window save/restore logic
enable-js.sh      standalone Chrome-setting enabler (accessibility click)
enable-js-restart.py  writes the pref directly, restarts Chrome, rebuilds the layout
whitecheck.m      pixel white-check helper (Objective-C / ScreenCaptureKit)
bin/whitecheck    compiled helper
logs/             rotating log
```
