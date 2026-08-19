#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""test-cycle — drive the real cycle() against a fake Chrome and count reloads.

Every never-catch bug found in the audit survived because the other tests exercise
probe_targets() and evaluate() in isolation, and cycle() is where the decision to
reload actually lives. These tests assert on OUTCOMES: did a reload happen, to which
windows, and how many cycles did it take.

    ./test-cycle.py
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("wg", os.path.join(HERE, "watchgrafana.py"))
wg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wg)

DASH = "https://grafana.client8.me/d/281e8816/core-metrics-arms-aliyun?orgId=1&from=now-6h"
TOP, BOT = 111, 222
fails = []


def check(name, ok, detail=""):
    print("%-6s %-52s %s" % ("PASS" if ok else "**FAIL**", name, detail[:58]))
    if not ok:
        fails.append(name)


def row(url=DASH, title="Core Metrics Arms - Aliyun", panels=16, charts=22, text=4000,
        left=0, right=956, err="", probe=True, active=1, top_px=0, mx=1170):
    pr = None
    if probe:
        pr = {"ok": True, "url": url, "path": "/d/x", "ready": "complete", "hidden": False,
              "login": "/login" in url, "text": text, "panels": panels, "charts": charts,
              "perrs": 0, "appErr": 0, "bg": "rgb(17,18,23)", "rows": [],
              "scroller": "HTML", "scrollTop": top_px, "scrollMax": mx}
    return {"left": left, "top": 25, "right": right, "bottom": 1080, "active_tab": active,
            "url": url, "title": title, "loading": False, "error": err, "probe": pr}


class FakeChrome(object):
    def __init__(self, rows_by_wid, snapshot_raises=False, batch_stalls=False):
        self.rows = rows_by_wid
        self.snapshot_raises = snapshot_raises
        self.batch_stalls = batch_stalls
        self.js_ok = True
        self.reloads = []
        self.set_urls = []
        self.snapshots = 0
        self.app = "Google Chrome"

    def batch(self, specs, code, timeout=None):
        if self.batch_stalls:
            raise wg.OsaError("timed out after %ss" % timeout, timed_out=True)
        return [self.rows[w] for w, _ in specs]

    def snapshot(self, needle):
        self.snapshots += 1
        if self.snapshot_raises:
            raise wg.OsaError("Google Chrome got an error: AppleEvent timed out", timed_out=True)
        out = []
        for wid, r in self.rows.items():
            matches = needle in (r["url"] or "")
            out.append({"id": wid, "left": r["left"], "top": r["top"], "right": r["right"],
                        "bottom": r["bottom"], "active_tab": r["active_tab"],
                        "match_tabs": [1] if matches else [], "minimized": False})
        return sorted(out, key=lambda w: w["left"])

    def reload(self, wid, ti):
        self.reloads.append(wid)

    def set_url(self, wid, ti, url):
        self.set_urls.append((wid, url))

    def activate_tab(self, wid, ti):
        pass

    def nudge(self, wid):
        pass

    def js_json(self, wid, ti, code, timeout=None):
        return {"ok": True, "how": "bottom", "want": 1170, "got": 1170, "max": 1170,
                "scrollHeight": 2250}


def base_cfg():
    cfg = wg.load_config()
    cfg["render_wait_seconds"] = 0.2
    cfg["render_poll_seconds"] = 0.05
    cfg["scroll_settle_delay"] = 0.0
    cfg["rediscover_seconds"] = 0
    cfg["roles"] = {"top": {"window_id": TOP, "scroll": "top"},
                    "bottom": {"window_id": BOT, "scroll": "bottom"}}
    return cfg


def run(ch, cfg, cycles=3, cache=None):
    state = {"bad_streak": {}, "recoveries": [], "notes": {}, "last_good_url": {}}
    cache = {"targets": {"top": (TOP, 1), "bottom": (BOT, 1)}} if cache is None else cache
    for _ in range(cycles):
        wg.cycle(cfg, ch, state, cache=cache)
    return state, cache


def main():
    wg.save_state = lambda *a, **k: None
    wg.save_config = lambda *a, **k: None
    wg.setup_logging(wg.load_config(), to_console=False)
    cfg = base_cfg()

    # ---- the regression the audit found: a URL that stopped matching
    ch = FakeChrome({TOP: row(url="chrome-error://chromewebdata/", title="Aw, Snap!",
                              probe=False, err="cannot run JS"),
                     BOT: row(left=964, right=1920)})
    run(ch, cfg, cycles=1)
    check("chrome-error:// reloads BOTH windows in one cycle",
          sorted(set(ch.reloads)) == [TOP, BOT], "reloads=%s" % sorted(set(ch.reloads)))

    ch = FakeChrome({TOP: row(url="https://ecs.console.alibabacloud.com/", title="ECS"),
                     BOT: row(left=964, right=1920)})
    run(ch, cfg, cycles=1)
    check("navigated-away reloads BOTH windows in one cycle",
          sorted(set(ch.reloads)) == [TOP, BOT], "reloads=%s" % sorted(set(ch.reloads)))

    # ---- a hard verdict must not be thrown away by re-discovery
    ch = FakeChrome({TOP: row(url="chrome-error://chromewebdata/", probe=False, err="x"),
                     BOT: row(left=964, right=1920)})
    run(ch, cfg, cycles=1)
    check("hard verdict is acted on, not re-resolved away", len(ch.reloads) >= 2,
          "%d reloads, %d snapshots" % (len(ch.reloads), ch.snapshots))

    # ---- wedged Chrome browser process: must still accumulate and act
    ch = FakeChrome({TOP: row(), BOT: row(left=964, right=1920)},
                    snapshot_raises=True, batch_stalls=True)
    state, _ = run(ch, cfg, cycles=3)
    check("wedged browser process still triggers a reload", len(ch.reloads) >= 2,
          "reloads=%s streak=%s" % (sorted(set(ch.reloads)), state["bad_streak"]))
    check("wedged browser process accumulates a streak", bool(state["bad_streak"]),
          "streak=%s" % state["bad_streak"])

    # ---- a genuinely blank page acts on the first check
    ch = FakeChrome({TOP: row(panels=0, charts=0, text=0), BOT: row(left=964, right=1920)})
    run(ch, cfg, cycles=1)
    check("blank page reloads both on the first check",
          sorted(set(ch.reloads)) == [TOP, BOT], "reloads=%s" % sorted(set(ch.reloads)))

    # ---- healthy must never reload (no false positives)
    ch = FakeChrome({TOP: row(), BOT: row(left=964, right=1920, top_px=1170)})
    run(ch, cfg, cycles=4)
    check("healthy windows are never reloaded", ch.reloads == [], "reloads=%s" % ch.reloads)

    # ---- half-rendered is confirmed first, not acted on immediately
    ch = FakeChrome({TOP: row(charts=0), BOT: row(left=964, right=1920, top_px=1170)})
    run(ch, cfg, cycles=1)
    first = list(ch.reloads)
    run(ch, cfg, cycles=2)
    check("half-rendered waits for a confirming check", first == [],
          "cycle1 reloads=%s" % first)

    # ---- hard re-navigation must never write an error URL into the tab
    ch = FakeChrome({TOP: row(url="chrome-error://chromewebdata/", probe=False, err="x"),
                     BOT: row(left=964, right=1920)})
    state = {"bad_streak": {}, "recoveries": [], "notes": {},
             "last_good_url": {"top": DASH, "bottom": DASH}}
    targets = {"top": (TOP, 1), "bottom": (BOT, 1)}
    results = wg.probe_targets(cfg, ch, targets)
    wg.recover(cfg, ch, targets, results, "top", ["forced"], hard=True,
               good_urls=state["last_good_url"])
    bad = [u for _, u in ch.set_urls if "chrome-error" in u]
    check("hard re-navigate never writes an error URL", not bad,
          "set_urls=%s" % [u[:42] for _, u in ch.set_urls])
    check("hard re-navigate uses the last known good URL",
          any(DASH in u for _, u in ch.set_urls) or ch.reloads != [],
          "set_urls=%d reloads=%d" % (len(ch.set_urls), len(ch.reloads)))

    # ---- a role with no window must keep being reported, not silently dropped
    ch = FakeChrome({BOT: row(left=964, right=1920, top_px=1170)})
    cfg2 = base_cfg()
    cfg2["rediscover_seconds"] = 0
    state, cache = run(ch, cfg2, cycles=3, cache={"targets": {"bottom": (BOT, 1)}})
    check("missing role keeps re-discovering, not locked in", ch.snapshots >= 3,
          "snapshots=%d (locked-in bug would give 0-1)" % ch.snapshots)

    print("\n%s" % ("all checks passed" if not fails
                    else "%d FAILED: %s" % (len(fails), ", ".join(fails))))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
