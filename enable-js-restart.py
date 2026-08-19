#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""enable-js-restart — turn on Chrome's "Allow JavaScript from Apple Events" by
writing the pref directly, since clicking the menu item under automation does
not take effect.

Chrome has to be restarted for the pref to be read, and Chrome on this machine
is not set to restore its session, so this saves every window's tabs and bounds
first and re-creates them afterwards.

    ./enable-js-restart.py --dry-run    show the layout it would save, change nothing
    ./enable-js-restart.py              do it
    ./enable-js-restart.py --enable-session-restore
                                        also switch Chrome to "Continue where you
                                        left off", so the wall rebuilds itself after
                                        any future restart, crash or reboot
    ./enable-js-restart.py --restore-only layout.json
                                        re-create windows from a saved layout
"""

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CHROME_DIR = os.path.expanduser("~/Library/Application Support/Google/Chrome")
LAYOUT = os.path.join(HERE, "layout.json")
PREF_PATH = ("browser", "allow_javascript_apple_events")
APP = "Google Chrome"
RS, US, TS = "\x1e", "\x1f", "\x1d"

NEWTAB = ("", "about:blank", "chrome://newtab/", "chrome://new-tab-page/",
          "chrome://new-tab-page", "chrome://newtab")


def osa(script, *args, timeout=40):
    p = subprocess.run(["/usr/bin/osascript", "-"] + [str(a) for a in args],
                       input=script, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "osascript failed").strip())
    return p.stdout.rstrip("\n")


SNAPSHOT = """
on run argv
  set RS to (character id 30)
  set US to (character id 31)
  set TS to (character id 29)
  set out to ""
  tell application "Google Chrome"
    repeat with w in windows
      set b to bounds of w
      set ai to 1
      try
        set ai to active tab index of w
      end try
      set tabList to ""
      repeat with t in tabs of w
        set u to ""
        try
          set u to URL of t
        end try
        if tabList is not "" then set tabList to tabList & TS
        set tabList to tabList & u
      end repeat
      set rec to (id of w as text) & US & (item 1 of b) & US & (item 2 of b) & US
      set rec to rec & (item 3 of b) & US & (item 4 of b) & US & (ai as text) & US & tabList
      set out to out & rec & RS
    end repeat
  end tell
  return out
end run
"""

MAKE_WINDOW = """
on run argv
  set u to item 1 of argv
  set l to (item 2 of argv) as integer
  set t to (item 3 of argv) as integer
  set r to (item 4 of argv) as integer
  set bt to (item 5 of argv) as integer
  tell application "Google Chrome"
    set w to make new window
    set URL of active tab of w to u
    delay 0.4
    set bounds of w to {l, t, r, bt}
    return (id of w as text)
  end tell
end run
"""

ADD_TAB = """
on run argv
  set wid to (item 1 of argv) as integer
  set u to item 2 of argv
  tell application "Google Chrome"
    set w to (first window whose id is wid)
    make new tab at end of tabs of w with properties {URL:u}
  end tell
  return "ok"
end run
"""

SET_BOUNDS = """
on run argv
  set wid to (item 1 of argv) as integer
  set l to (item 2 of argv) as integer
  set t to (item 3 of argv) as integer
  set r to (item 4 of argv) as integer
  set bt to (item 5 of argv) as integer
  tell application "Google Chrome"
    set bounds of (first window whose id is wid) to {l, t, r, bt}
  end tell
  return "ok"
end run
"""

ACTIVATE_TAB = """
on run argv
  set wid to (item 1 of argv) as integer
  set ti to (item 2 of argv) as integer
  tell application "Google Chrome"
    set w to (first window whose id is wid)
    if ti <= (count of tabs of w) then set active tab index of w to ti
  end tell
  return "ok"
end run
"""

CLOSE_WINDOW = """
on run argv
  set wid to (item 1 of argv) as integer
  tell application "Google Chrome"
    close (first window whose id is wid)
  end tell
  return "ok"
end run
"""

JS_TEST = """
on run argv
  tell application "Google Chrome"
    return (execute (active tab of front window) javascript "1+1") as text
  end tell
end run
"""


def snapshot():
    out = osa(SNAPSHOT)
    wins = []
    for rec in out.split(RS):
        if not rec.strip():
            continue
        f = rec.split(US)
        if len(f) < 7:
            continue
        wins.append({"id": int(f[0]), "bounds": [int(f[1]), int(f[2]), int(f[3]), int(f[4])],
                     "active_tab": int(f[5]) or 1, "tabs": f[6].split(TS) if f[6] else []})
    return wins


def describe(wins, needle="core-metrics-arms-aliyun"):
    n_tabs = sum(len(w["tabs"]) for w in wins)
    print("   %d windows, %d tabs" % (len(wins), n_tabs))
    for w in wins:
        b = w["bounds"]
        dash = any(needle in u for u in w["tabs"])
        print("     %-11s (%d,%d) %dx%d  tab %d/%d  %s" % (
            w["id"], b[0], b[1], b[2] - b[0], b[3] - b[1], w["active_tab"], len(w["tabs"]),
            "<- dashboard" if dash else ""))


def chrome_running():
    p = subprocess.run(["/usr/bin/pgrep", "-x", APP], capture_output=True, text=True)
    return p.returncode == 0


def pref_files():
    out = [os.path.join(CHROME_DIR, "Local State")]
    for d in sorted(os.listdir(CHROME_DIR)):
        p = os.path.join(CHROME_DIR, d, "Preferences")
        if os.path.exists(p):
            out.append(p)
    return [p for p in out if os.path.exists(p)]


def patch_prefs(files, stamp, session_restore=False):
    done = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception as exc:
            print("   skip %s (%s)" % (os.path.relpath(f, CHROME_DIR), exc))
            continue
        bak = "%s.bak-%s" % (f, stamp)
        with open(bak, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        d.setdefault(PREF_PATH[0], {})[PREF_PATH[1]] = True
        if session_restore and os.path.basename(f) == "Preferences":
            # 1 = "Continue where you left off": Chrome then rebuilds the wall itself
            # after any restart, crash or reboot.
            d.setdefault("session", {})["restore_on_startup"] = 1
            print("   set session.restore_on_startup = 1 in %s"
                  % os.path.relpath(f, CHROME_DIR))
        tmp = f + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, separators=(",", ":"))
        os.replace(tmp, f)
        done.append(f)
        print("   set %s.%s = true in %s  (backup: %s)" % (
            PREF_PATH[0], PREF_PATH[1], os.path.relpath(f, CHROME_DIR),
            os.path.basename(bak)))
    return done


def js_ok():
    try:
        return osa(JS_TEST, timeout=20).strip() == "2"
    except Exception:
        return False


def restore(saved):
    """Re-create anything Chrome did not bring back, without duplicating what it did.

    Matching is per-window, not per-URL: the two monitoring windows show the SAME
    dashboard URL, so a global "is this URL open anywhere" test would collapse
    them into one.
    """
    time.sleep(1.0)
    unclaimed = list(snapshot())

    for w in saved:
        wanted = [u for u in w["tabs"] if u and u not in NEWTAB]
        if not wanted:
            continue

        # claim the unclaimed window sharing the most URLs with this saved one
        best, best_overlap = None, 0
        for cur in unclaimed:
            overlap = len(set(cur["tabs"]) & set(wanted))
            if overlap > best_overlap:
                best, best_overlap = cur, overlap

        if best is not None:
            unclaimed.remove(best)
            for u in [x for x in wanted if x not in set(best["tabs"])]:
                try:
                    osa(ADD_TAB, best["id"], u, timeout=30)
                except Exception as exc:
                    print("   could not re-add %s: %s" % (u[:60], exc))
            try:
                osa(SET_BOUNDS, best["id"], *w["bounds"])
            except Exception as exc:
                print("   could not set bounds on %s: %s" % (best["id"], exc))
            try:
                osa(ACTIVATE_TAB, best["id"], w["active_tab"])
            except Exception:
                pass
            print("   window %s came back on its own, geometry and tabs re-applied" % best["id"])
            continue

        try:
            new_id = int(osa(MAKE_WINDOW, wanted[0], *w["bounds"], timeout=60))
        except Exception as exc:
            print("   could not re-create a window for %s: %s" % (wanted[0][:60], exc))
            continue
        for u in wanted[1:]:
            try:
                osa(ADD_TAB, new_id, u, timeout=30)
            except Exception as exc:
                print("   could not re-add %s: %s" % (u[:60], exc))
        try:
            osa(ACTIVATE_TAB, new_id, w["active_tab"])
        except Exception:
            pass
        print("   re-created window %s with %d tab(s) at (%d,%d) %dx%d" % (
            new_id, len(wanted), w["bounds"][0], w["bounds"][1],
            w["bounds"][2] - w["bounds"][0], w["bounds"][3] - w["bounds"][1]))

    # drop the empty New Tab window Chrome opens on launch
    for w in snapshot():
        if len(w["tabs"]) == 1 and w["tabs"][0] in NEWTAB:
            try:
                osa(CLOSE_WINDOW, w["id"])
                print("   closed the empty New Tab window Chrome opened on launch")
            except Exception:
                pass


def main():
    args = sys.argv[1:]

    if "--restore-only" in args:
        i = args.index("--restore-only")
        path = args[i + 1] if len(args) > i + 1 else LAYOUT
        with open(path) as fh:
            saved = json.load(fh)
        print("restoring from %s" % path)
        restore(saved)
        return 0

    if not os.path.isdir(CHROME_DIR):
        print("Chrome profile directory not found: %s" % CHROME_DIR)
        return 1

    print("reading the current layout ...")
    saved = snapshot()
    describe(saved)
    with open(LAYOUT, "w", encoding="utf-8") as fh:
        json.dump(saved, fh, indent=2, ensure_ascii=False)
    print("   saved to %s" % LAYOUT)

    if js_ok():
        print("\n[ok] Chrome already runs JavaScript from Apple Events — nothing to do.")
        return 0

    if "--dry-run" in args:
        print("\nfiles that would be patched:")
        for f in pref_files():
            print("   %s" % os.path.relpath(f, CHROME_DIR))
        if "--enable-session-restore" in args:
            print("   ...and session.restore_on_startup would be set to 1")
        print("\ndry run: nothing changed. Re-run without --dry-run to go ahead.")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    print("\nquitting Chrome ...")
    try:
        osa('on run argv\n  tell application "Google Chrome" to quit\nend run', timeout=30)
    except Exception as exc:
        print("   quit request failed: %s" % exc)
    for _ in range(40):
        if not chrome_running():
            break
        time.sleep(0.5)
    if chrome_running():
        print("   Chrome is still running after 20s — aborting, nothing was changed.")
        print("   Quit Chrome by hand, then re-run this script.")
        return 1
    print("   Chrome stopped")

    print("\npatching preferences ...")
    if not patch_prefs(pref_files(), stamp, session_restore="--enable-session-restore" in args):
        print("   nothing could be patched — aborting")
        return 1

    print("\nrelaunching Chrome ...")
    subprocess.run(["/usr/bin/open", "-a", APP], capture_output=True)
    ok = False
    for _ in range(60):
        time.sleep(0.5)
        try:
            snapshot()
            ok = True
            break
        except Exception:
            continue
    if not ok:
        print("   Chrome did not come back as scriptable within 30s.")
        print("   Once it is up, restore your windows with:")
        print("     ./enable-js-restart.py --restore-only %s" % LAYOUT)
        return 1
    print("   Chrome is back")

    print("\nrestoring windows ...")
    restore(saved)

    print()
    if js_ok():
        print("[ok] Chrome now runs JavaScript from Apple Events.")
        print("     Next:  ./watchgrafana.py doctor && ./watchgrafana.py probe")
        print("     The running watchdog re-pins the windows by itself on its next cycle.")
        return 0
    print("[FAIL] The pref is written but Chrome still refuses AppleScript JavaScript.")
    print("       Backups are alongside the originals as *.bak-%s" % stamp)
    print("       Your layout is saved in %s" % LAYOUT)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted — if Chrome is down, relaunch it and run:")
        print("  ./enable-js-restart.py --restore-only %s" % LAYOUT)
        sys.exit(130)
