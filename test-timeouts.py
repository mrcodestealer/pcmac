#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""test-timeouts — the hung-renderer path, which is the case that matters most.

A white or wedged tab is precisely one that stops answering JavaScript, so the
probe timeout IS the detection latency. This pins down that a stall costs one
timeout rather than one per window, and that an unread URL is never mistaken for
a tab that navigated away.

    ./test-timeouts.py
"""

import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("wg", os.path.join(HERE, "watchgrafana.py"))
wg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wg)

fails = []


def check(name, ok, detail=""):
    print("%-6s %-50s %s" % ("PASS" if ok else "**FAIL**", name, detail[:60]))
    if not ok:
        fails.append(name)


class HungChrome(object):
    """Tabs that never answer, burning the full deadline like a real stall."""

    def __init__(self):
        self.calls = 0
        self.js_ok = None

    def batch(self, specs, code, timeout=None):
        self.calls += 1
        time.sleep(timeout)
        raise wg.OsaError("timed out after %ss" % timeout, timed_out=True)


class BrokenChrome(object):
    """Replies, but malformed — a different failure that SHOULD retry per window."""

    def __init__(self):
        self.calls = 0
        self.js_ok = None

    def batch(self, specs, code, timeout=None):
        self.calls += 1
        if len(specs) > 1:
            raise wg.OsaError("mangled reply", timed_out=False)
        return [wg._blank_row("")]


def main():
    cfg = wg.load_config()
    wg.setup_logging(cfg, to_console=False)
    cfg["probe_timeout_seconds"] = 2  # keep the test quick
    targets = {"top": (111, 1), "bottom": (222, 1)}

    ch = HungChrome()
    t = time.time()
    res = wg.probe_targets(cfg, ch, targets)
    elapsed = time.time() - t

    check("a stall costs ONE timeout, not one per window", ch.calls == 1,
          "batch calls = %d" % ch.calls)
    check("elapsed is a single timeout", 1.8 <= elapsed <= 3.2,
          "%.2fs" % elapsed)
    check("every window is reported unhealthy",
          all(not r["healthy"] for r in res.values()))
    check("the stall is the only reason given",
          all(r["reasons"] == ["page did not answer the health probe "
                              "(renderer stopped answering (probe deadline 2s/tab))"]
              for r in res.values()),
          "; ".join(res["top"]["reasons"])[:60])
    check("a stall needs confirming, so one slow render cannot reload",
          all(not r["hard"] for r in res.values()))

    ch2 = BrokenChrome()
    wg.probe_targets(cfg, ch2, targets)
    check("a malformed reply still retries per window", ch2.calls == 3,
          "batch calls = %d" % ch2.calls)

    meta = {"url": "https://ecs.console.alibabacloud.com/", "title": "ECS", "loading": False}
    healthy, reasons, hard = wg.evaluate(cfg, "top", meta, None, None)
    check("a genuine navigate-away is still immediate", (not healthy) and hard,
          "; ".join(reasons)[:60])

    meta = {"url": "", "title": "", "loading": False}
    healthy, reasons, hard = wg.evaluate(cfg, "top", meta, None, None)
    check("an unread URL is not read as navigate-away",
          not any("navigated away" in r for r in reasons), "; ".join(reasons)[:60])

    print("\n%s" % ("all checks passed" if not fails
                    else "%d FAILED: %s" % (len(fails), ", ".join(fails))))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
