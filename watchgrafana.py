#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
watchgrafana — keep two Chrome windows pinned on a Grafana dashboard.

One window shows the TOP of the dashboard, the other the BOTTOM. When either
window goes blank/white (Grafana SPA crash, dead renderer, error page, hung
load), both windows are reloaded and each is scrolled back to its assigned
part of the dashboard.

Requires (see `watchgrafana doctor`):
  * Automation permission for Chrome  (granted the first time you run it)
  * Chrome menu bar -> View -> Developer -> Allow JavaScript from Apple Events
  * optional: Screen Recording, only for the extra pixel-level white check
"""

import json
import os
import subprocess
import sys
import time
import logging
import logging.handlers
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
WHITECHECK = os.path.join(HERE, "bin", "whitecheck")
WHITECHECK_SRC = os.path.join(HERE, "whitecheck.m")

RS = "\x1e"  # record separator used by the AppleScript snapshot
US = "\x1f"  # field separator

DEFAULTS = {
    "browser_app": "Google Chrome",
    "url_contains": "core-metrics-arms-aliyun",
    "role_order": ["top", "bottom"],
    "roles": {
        "top":    {"window_id": None, "scroll": "top"},
        "bottom": {"window_id": None, "scroll": "bottom"},
    },
    "interval_seconds": 5,
    "activate_tab_on_recovery": True,
    "steady_scroll": "on_reset",          # off | on_reset | always
    "scroll_reset_epsilon_px": 60,
    "scroll_tolerance_px": 200,
    "bad_checks_before_action": 2,         # for ambiguous signals only; a clearly
                                           # blank/crashed page acts on the first check
    "cooldown_seconds": 90,                # min gap between recoveries
    "max_recoveries_per_hour": 12,
    "render_wait_seconds": 30,             # how long to wait for panels after a reload
    "render_poll_seconds": 0.6,            # how often to re-check while waiting
    "scroll_settle_delay": 0.5,            # pause between scroll re-applications
    "min_panels": 1,
    "rediscover_seconds": 30,              # min gap between full window enumerations
    "dashboard_url": "",                   # optional; used to re-navigate a lost tab.
                                           # Left blank, the last URL seen healthy is used.
    "min_body_text": 60,
    "osascript_timeout": 20,               # control actions (reload, re-navigate)
    "probe_timeout_seconds": 2,            # health probe; a healthy one answers in ~0.5s,
                                           # so a stall here IS the symptom, not an excuse
                                           # to keep waiting
    "pixel_check": "off",                  # off | auto | on
    "white_frac_threshold": 0.80,
    "log_file": "logs/watchgrafana.log",
    "log_max_bytes": 2000000,
    "log_backups": 3,
}

# --------------------------------------------------------------------------
# config / state
# --------------------------------------------------------------------------


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    raw = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as fh:
                raw = json.load(fh)
        except Exception as exc:
            print("config.json is not valid JSON: %s" % exc, file=sys.stderr)
            sys.exit(1)
    return deep_merge(DEFAULTS, raw)


def save_config(cfg):
    """Persist role pinning without reformatting or dropping the user's keys."""
    disk = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as fh:
                disk = json.load(fh)
        except Exception:
            disk = {}
    disk.setdefault("url_contains", cfg["url_contains"])
    disk.setdefault("role_order", cfg["role_order"])
    roles = disk.setdefault("roles", {})
    for role, spec in cfg["roles"].items():
        entry = roles.setdefault(role, {})
        entry["window_id"] = spec.get("window_id")
        entry.setdefault("scroll", spec.get("scroll", "top"))
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(disk, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as fh:
                return json.load(fh)
        except Exception:
            pass
    return {"recoveries": [], "bad_streak": {}, "notes": {}}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)


LOG = logging.getLogger("watchgrafana")


def setup_logging(cfg, to_console=True):
    LOG.setLevel(logging.INFO)
    LOG.handlers = []
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    path = cfg["log_file"]
    if not os.path.isabs(path):
        path = os.path.join(HERE, path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        path, maxBytes=cfg["log_max_bytes"], backupCount=cfg["log_backups"])
    fh.setFormatter(fmt)
    LOG.addHandler(fh)
    if to_console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        LOG.addHandler(ch)
    return path


# --------------------------------------------------------------------------
# AppleScript bridge
# --------------------------------------------------------------------------


class OsaError(Exception):
    def __init__(self, msg, timed_out=False):
        super().__init__(msg)
        self.timed_out = timed_out
        self.msg = msg


JS_DISABLED_MARK = "Executing JavaScript through AppleScript is turned off"


def osa(script, *args, timeout=20):
    """Run an AppleScript with `on run argv`, passing args positionally."""
    cmd = ["/usr/bin/osascript", "-"] + [str(a) for a in args]
    try:
        p = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OsaError("osascript timed out after %ss" % timeout, timed_out=True)
    if p.returncode != 0:
        raise OsaError((p.stderr or "osascript failed").strip())
    return p.stdout.rstrip("\n")


SNAPSHOT_SCPT = """
on run argv
  set needle to item 1 of argv
  set appName to item 2 of argv
  set RS to (character id 30)
  set US to (character id 31)
  set out to ""
  using terms from application "Google Chrome"
    tell application appName
      repeat with w in windows
        set b to bounds of w
        set ai to 0
        try
          set ai to active tab index of w
        end try
        set hits to ""
        set idx to 0
        repeat with t in tabs of w
          set idx to idx + 1
          set u to ""
          try
            set u to URL of t
          end try
          if u contains needle then
            if hits is not "" then set hits to hits & ","
            set hits to hits & idx
          end if
        end repeat
        set mz to "false"
        try
          if minimized of w then set mz to "true"
        end try
        set rec to (id of w as text) & US & (item 1 of b) & US & (item 2 of b) & US
        set rec to rec & (item 3 of b) & US & (item 4 of b) & US & (ai as text) & US
        set rec to rec & hits & US & mz
        set out to out & rec & RS
      end repeat
    end tell
  end using terms from
  return out
end run
"""

BATCH_SCPT = """
on run argv
  set appName to item 1 of argv
  set js to item 2 of argv
  set tmo to (item 3 of argv) as integer
  set US to (character id 31)
  set RS to (character id 30)
  set out to ""
  with timeout of tmo seconds
    using terms from application "Google Chrome"
      tell application appName
        repeat with i from 4 to (count of argv)
          set spec to item i of argv
          set oldTID to AppleScript's text item delimiters
          set AppleScript's text item delimiters to ","
          set parts to text items of spec
          set AppleScript's text item delimiters to oldTID
          set wid to (item 1 of parts) as integer
          set ti to (item 2 of parts) as integer
          set lft to 0
          set tp to 0
          set rgt to 0
          set btm to 0
          set ai to 0
          set u to ""
          set ttl to ""
          set ld to "false"
          set jsres to ""
          set errs to ""
          try
            set theWin to (first window whose id is wid)
            set b to bounds of theWin
            set lft to item 1 of b
            set tp to item 2 of b
            set rgt to item 3 of b
            set btm to item 4 of b
            try
              set ai to active tab index of theWin
            end try
            set t to tab ti of theWin
            try
              set u to URL of t
            end try
            try
              set ttl to title of t
            end try
            try
              if loading of t then set ld to "true"
            end try
            try
              set jsres to (execute t javascript js)
            on error e
              set errs to e
            end try
          on error e2
            set errs to e2
          end try
          set rec to (lft as text) & US & (tp as text) & US & (rgt as text) & US
          set rec to rec & (btm as text) & US & (ai as text) & US & u & US & ttl
          set rec to rec & US & ld & US & errs & US & jsres
          set out to out & rec & RS
        end repeat
      end tell
    end using terms from
  end timeout
  return out
end run
"""

TAB_META_SCPT = """
on run argv
  set appName to item 1 of argv
  set wid to (item 2 of argv) as integer
  set ti to (item 3 of argv) as integer
  set US to (character id 31)
  using terms from application "Google Chrome"
    tell application appName
      set theWin to (first window whose id is wid)
      set t to tab ti of theWin
      set ld to "false"
      try
        if loading of t then set ld to "true"
      end try
      return (URL of t) & US & (title of t) & US & ld
    end tell
  end using terms from
end run
"""

EXEC_JS_SCPT = """
on run argv
  set appName to item 1 of argv
  set wid to (item 2 of argv) as integer
  set ti to (item 3 of argv) as integer
  set js to item 4 of argv
  set tmo to (item 5 of argv) as integer
  with timeout of tmo seconds
    using terms from application "Google Chrome"
      tell application appName
        set theWin to (first window whose id is wid)
        return execute (tab ti of theWin) javascript js
      end tell
    end using terms from
  end timeout
end run
"""

RELOAD_SCPT = """
on run argv
  set appName to item 1 of argv
  set wid to (item 2 of argv) as integer
  set ti to (item 3 of argv) as integer
  with timeout of 30 seconds
    using terms from application "Google Chrome"
      tell application appName
        set theWin to (first window whose id is wid)
        reload (tab ti of theWin)
      end tell
    end using terms from
  end timeout
  return "ok"
end run
"""

SET_URL_SCPT = """
on run argv
  set appName to item 1 of argv
  set wid to (item 2 of argv) as integer
  set ti to (item 3 of argv) as integer
  set u to item 4 of argv
  with timeout of 30 seconds
    using terms from application "Google Chrome"
      tell application appName
        set theWin to (first window whose id is wid)
        set URL of (tab ti of theWin) to u
      end tell
    end using terms from
  end timeout
  return "ok"
end run
"""

ACTIVATE_TAB_SCPT = """
on run argv
  set appName to item 1 of argv
  set wid to (item 2 of argv) as integer
  set ti to (item 3 of argv) as integer
  using terms from application "Google Chrome"
    tell application appName
      set theWin to (first window whose id is wid)
      set active tab index of theWin to ti
    end tell
  end using terms from
  return "ok"
end run
"""

ENABLE_JS_SCPT = """
on run argv
  set appName to item 1 of argv
  tell application appName to activate
  delay 0.5
  tell application "System Events"
    tell process appName
      set devMenu to menu "Developer" of menu item "Developer" of menu "View" of menu bar item "View" of menu bar 1
      set seen to ""
      set target to missing value
      repeat with mi in (every menu item of devMenu)
        set n to ""
        try
          set n to name of mi
        end try
        if n is not "" then
          if seen is not "" then set seen to seen & " | "
          set seen to seen & n
          if n contains "Apple Events" then set target to mi
        end if
      end repeat
      if target is missing value then
        try
          key code 53
        end try
        return "NOTFOUND: " & seen
      end if
      set nm to name of target
      set mk1 to "none"
      try
        set mk1 to (value of attribute "AXMenuItemMarkChar" of target) as text
      end try
      click target
      delay 0.8
      set mk2 to "none"
      try
        set mk2 to (value of attribute "AXMenuItemMarkChar" of target) as text
      end try
      try
        key code 53
      end try
      return "CLICKED: " & nm & " (checkmark " & mk1 & " -> " & mk2 & ")"
    end tell
  end tell
end run
"""

NUDGE_SCPT = """
on run argv
  set appName to item 1 of argv
  set wid to (item 2 of argv) as integer
  using terms from application "Google Chrome"
    tell application appName
      set theWin to (first window whose id is wid)
      set b to bounds of theWin
      set bounds of theWin to {item 1 of b, item 2 of b, (item 3 of b) - 2, (item 4 of b) - 2}
      delay 0.2
      set bounds of theWin to b
    end tell
  end using terms from
  return "ok"
end run
"""


class Chrome(object):
    def __init__(self, cfg):
        self.app = cfg["browser_app"]
        self.timeout = cfg["osascript_timeout"]
        self.js_ok = None  # None = unknown, True/False once probed

    def snapshot(self, needle):
        out = osa(SNAPSHOT_SCPT, needle, self.app, timeout=self.timeout)
        wins = []
        for rec in out.split(RS):
            rec = rec.strip("\n")
            if not rec:
                continue
            f = rec.split(US)
            if len(f) < 8:
                continue
            hits = [int(x) for x in f[6].split(",") if x]
            wins.append({
                "id": int(f[0]),
                "left": int(f[1]), "top": int(f[2]), "right": int(f[3]), "bottom": int(f[4]),
                "active_tab": int(f[5]) if f[5] else 0,
                "match_tabs": hits,
                "minimized": f[7] == "true",
            })
        return wins

    def tab_meta(self, wid, ti):
        out = osa(TAB_META_SCPT, self.app, wid, ti, timeout=self.timeout)
        f = out.split(US)
        while len(f) < 3:
            f.append("")
        return {"url": f[0], "title": f[1], "loading": f[2] == "true"}

    def js(self, wid, ti, code, timeout=None):
        t = timeout or self.timeout
        try:
            out = osa(EXEC_JS_SCPT, self.app, wid, ti, code, t, timeout=t + 5)
        except OsaError as exc:
            if JS_DISABLED_MARK in exc.msg:
                self.js_ok = False
                raise
            raise
        self.js_ok = True
        return out

    def js_json(self, wid, ti, code, timeout=None):
        raw = self.js(wid, ti, code, timeout=timeout)
        raw = raw.strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            LOG.debug("non-JSON JS reply: %r", raw[:400])
            return None

    def batch(self, specs, code, timeout=None):
        """One osascript round-trip for every monitored tab: geometry, tab metadata
        and the in-page health probe together. Five spawns per cycle become one."""
        t = timeout or self.timeout
        args = [self.app, code, t] + ["%d,%d" % (w, i) for w, i in specs]
        out = osa(BATCH_SCPT, *args, timeout=t + 5)
        rows = []
        for rec in out.split(RS):
            if not rec.strip():
                continue
            f = rec.split(US)
            while len(f) < 10:
                f.append("")
            err = f[8]
            if err and JS_DISABLED_MARK in err:
                self.js_ok = False
            elif f[9]:
                self.js_ok = True
            probe = None
            if f[9]:
                try:
                    probe = json.loads(f[9])
                except Exception:
                    probe = None
            rows.append({
                "left": int(f[0] or 0), "top": int(f[1] or 0),
                "right": int(f[2] or 0), "bottom": int(f[3] or 0),
                "active_tab": int(f[4] or 0),
                "url": f[5], "title": f[6], "loading": f[7] == "true",
                "error": err, "probe": probe,
            })
        return rows

    def reload(self, wid, ti):
        osa(RELOAD_SCPT, self.app, wid, ti, timeout=self.timeout + 15)

    def set_url(self, wid, ti, url):
        osa(SET_URL_SCPT, self.app, wid, ti, url, timeout=self.timeout + 15)

    def activate_tab(self, wid, ti):
        osa(ACTIVATE_TAB_SCPT, self.app, wid, ti, timeout=self.timeout)

    def nudge(self, wid):
        osa(NUDGE_SCPT, self.app, wid, timeout=self.timeout)


# --------------------------------------------------------------------------
# injected JavaScript
# --------------------------------------------------------------------------

# Shared helpers. Grafana's dashboard does not scroll the document itself; it
# scrolls an inner container whose class name changes between versions, so find
# it by behaviour rather than by name.
JS_PRELUDE = r"""
var __wg = (function(){
  function candidates(){
    var out = [], sels = [
      '[data-testid="scrollbar-view"]', '.scrollbar-view',
      '[data-testid="data-testid Dashboard canvas"]',
      '#pageContent', '.main-view', '.dashboard-container',
      'main', '[role="main"]'
    ];
    for (var i = 0; i < sels.length; i++) {
      var els = document.querySelectorAll(sels[i]);
      for (var j = 0; j < els.length; j++) out.push(els[j]);
    }
    if (document.scrollingElement) out.push(document.scrollingElement);
    if (document.body) out.push(document.body);
    return out;
  }
  function span(e){ return e ? (e.scrollHeight - e.clientHeight) : 0; }
  function pickScroller(){
    var best = null, bestSpan = 40, c = candidates(), i;
    for (i = 0; i < c.length; i++) if (span(c[i]) > bestSpan) { best = c[i]; bestSpan = span(c[i]); }
    if (best) return best;
    // fallback: sweep for any scrollable block
    var all = document.querySelectorAll('div,main,section');
    for (i = 0; i < all.length; i++) {
      var e = all[i];
      if (span(e) > bestSpan) {
        var ov = getComputedStyle(e).overflowY;
        if (ov === 'auto' || ov === 'scroll') { best = e; bestSpan = span(e); }
      }
    }
    return best;
  }
  function rowTitles(){
    var sels = [
      '[data-testid^="data-testid dashboard row title"]',
      '[data-testid^="data-testid Dashboard row title"]',
      '.dashboard-row__title', '.dashboard-row button', 'h2, h3'
    ];
    var out = [], seen = [];
    for (var i = 0; i < sels.length; i++) {
      var els = document.querySelectorAll(sels[i]);
      for (var j = 0; j < els.length; j++) {
        var e = els[j], t = (e.innerText || e.textContent || '').trim();
        if (!t || t.length > 80) continue;
        if (seen.indexOf(e) >= 0) continue;
        seen.push(e); out.push({ el: e, text: t });
      }
    }
    return out;
  }
  function findRow(needle){
    var rows = rowTitles(), n = needle.toLowerCase(), i;
    for (i = 0; i < rows.length; i++)
      if (rows[i].text.toLowerCase() === n) return rows[i].el;
    for (i = 0; i < rows.length; i++)
      if (rows[i].text.toLowerCase().indexOf(n) >= 0) return rows[i].el;
    return null;
  }
  return { pickScroller: pickScroller, span: span, rowTitles: rowTitles, findRow: findRow };
})();
"""

JS_PROBE = JS_PRELUDE + r"""
JSON.stringify((function(){
  var r = { probe: 1 };
  try {
    r.url    = location.href;
    r.path   = location.pathname;
    r.ready  = document.readyState;
    r.hidden = !!document.hidden;
    r.login  = /\/login/.test(location.pathname);
    var body = document.body;
    r.text   = body ? (body.innerText || '').trim().length : 0;
    r.panels = (function(){
      var seen = [], hits = document.querySelectorAll(
        '[data-testid="data-testid panel content"], [data-panelid], .panel-container');
      for (var i = 0; i < hits.length; i++) {
        // collapse nested matches onto the panel root so each panel counts once
        var root = hits[i].closest('[data-panelid], .panel-container') || hits[i];
        if (seen.indexOf(root) < 0) seen.push(root);
      }
      return seen.length;
    })();
    r.charts = document.querySelectorAll('canvas, .u-plot, svg.uplot, .panel-content svg').length;
    r.perrs  = document.querySelectorAll(
      '[data-testid="data-testid Panel status error"], .panel-info-corner--error').length;
    r.appErr = /An unexpected error happened|Something went wrong/i.test(
      body ? (body.innerText || '').slice(0, 4000) : '') ? 1 : 0;
    r.bg     = body ? getComputedStyle(body).backgroundColor : '';
    r.rows   = __wg.rowTitles().map(function(x){ return x.text; }).slice(0, 40);
    var sc = __wg.pickScroller();
    if (sc) {
      r.scroller  = (sc.tagName || '?') + (sc.className ? '.' + String(sc.className).split(' ')[0] : '');
      r.scrollTop = Math.round(sc.scrollTop);
      r.scrollMax = Math.round(sc.scrollHeight - sc.clientHeight);
    } else {
      r.scroller = null; r.scrollTop = 0; r.scrollMax = 0;
    }
    r.ok = true;
  } catch (e) {
    r.ok = false; r.jsErr = String(e);
  }
  return r;
})())
"""


def js_scroll(target):
    """Build the scroll script for a target descriptor."""
    return JS_PRELUDE + r"""
JSON.stringify((function(){
  var target = %s;
  var sc = __wg.pickScroller();
  if (!sc) return { ok: false, reason: 'no-scroller' };
  var max = Math.max(0, sc.scrollHeight - sc.clientHeight), want = null, how = target;
  if (target === 'top') want = 0;
  else if (target === 'bottom') want = max;
  else if (target.indexOf('fraction:') === 0) want = max * parseFloat(target.slice(9));
  else if (target.indexOf('px:') === 0) want = parseFloat(target.slice(3));
  else if (target.indexOf('row:') === 0) {
    var el = __wg.findRow(target.slice(4));
    if (el) {
      var er = el.getBoundingClientRect(), sr = sc.getBoundingClientRect();
      want = sc.scrollTop + (er.top - sr.top) - 8;
      how = 'row-hit';
    } else {
      want = max; how = 'row-miss->bottom';
    }
  } else want = max;
  want = Math.max(0, Math.min(max, Math.round(want)));
  sc.scrollTop = want;
  if (sc === document.scrollingElement || sc === document.body) window.scrollTo(0, want);
  return { ok: true, how: how, want: want, got: Math.round(sc.scrollTop), max: max,
           scrollHeight: sc.scrollHeight };
})())
""" % json.dumps(target)


# --------------------------------------------------------------------------
# health evaluation
# --------------------------------------------------------------------------

CHROME_ERROR_TITLES = ("aw, snap", "aw snap", "no data received", "site can't be reached",
                       "site cannot be reached", "not available", "connection reset",
                       "err_", "problem loading page", "untitled")


def looks_like_error_page(meta):
    url = (meta.get("url") or "").lower()
    title = (meta.get("title") or "").lower()
    if url.startswith("chrome-error:") or url.startswith("chrome://network-error"):
        return "chrome error page (%s)" % url[:60]
    for frag in CHROME_ERROR_TITLES:
        if frag in title:
            return "browser error page (title=%r)" % meta.get("title")
    return None


def evaluate(cfg, role, meta, probe, white):
    """Return (healthy, reasons, hard).

    `hard` marks a failure that is unambiguous on a single observation - a crashed
    tab, a login bounce, a page with neither text nor panels. Those are acted on at
    once. Everything else (a half-rendered dashboard, a renderer that missed one
    probe, a white-looking window) waits for `bad_checks_before_action` in a row, so
    a slow refresh never triggers a reload.
    """
    bad = []  # (reason, hard)

    err = looks_like_error_page(meta)
    if err:
        bad.append((err, True))

    # Only judge the URL when we actually read one. A stalled probe reports no URL,
    # and an unread URL is not evidence the tab went anywhere.
    url = meta.get("url") or ""
    if url and cfg["url_contains"] not in url:
        bad.append(("tab navigated away from the dashboard (%s)" % url[:80], True))

    if probe is None:
        # JS unavailable or it threw: only a signal when JS normally works
        if meta.get("probe_error"):
            bad.append(("page did not answer the health probe (%s)"
                        % meta["probe_error"], False))
    else:
        if not probe.get("ok"):
            bad.append(("probe error: %s" % probe.get("jsErr"), True))
        if probe.get("login"):
            bad.append(("bounced to the Grafana login page", True))
        if probe.get("appErr"):
            bad.append(("Grafana rendered its error screen", True))

        blank_text = probe.get("text", 0) < cfg["min_body_text"]
        no_panels = probe.get("panels", 0) < cfg["min_panels"]
        if blank_text and no_panels:
            bad.append(("page is completely blank (%s chars of text, no panels)"
                        % probe.get("text"), True))
        else:
            if blank_text:
                bad.append(("almost no text on the page (%s chars)" % probe.get("text"), False))
            if no_panels:
                bad.append(("no dashboard panels in the DOM", False))
            elif probe.get("charts", 0) < 1 and not probe.get("hidden"):
                bad.append(("panels present but nothing rendered inside them", False))

    if white is not None and white >= cfg["white_frac_threshold"]:
        bad.append(("window is %d%% white on screen" % round(white * 100), False))

    return (not bad), [t for t, _ in bad], any(h for _, h in bad)


# --------------------------------------------------------------------------
# pixel check
# --------------------------------------------------------------------------


def ensure_whitecheck():
    if os.path.exists(WHITECHECK) and os.path.getmtime(WHITECHECK) >= os.path.getmtime(WHITECHECK_SRC):
        return True
    if not os.path.exists(WHITECHECK_SRC):
        return False
    os.makedirs(os.path.dirname(WHITECHECK), exist_ok=True)
    p = subprocess.run(["/usr/bin/clang", "-fobjc-arc", "-O2",
                        "-framework", "Foundation", "-framework", "CoreGraphics",
                        "-framework", "ScreenCaptureKit", "-framework", "CoreMedia",
                        "-o", WHITECHECK, WHITECHECK_SRC],
                       capture_output=True, text=True)
    if p.returncode != 0:
        LOG.warning("could not build whitecheck: %s", (p.stderr or "").strip()[:300])
        return False
    return True


def whitecheck(args, timeout=25):
    if not ensure_whitecheck():
        return None, "whitecheck helper unavailable"
    try:
        p = subprocess.run([WHITECHECK] + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "whitecheck timed out"
    try:
        data = json.loads(p.stdout or "{}")
    except Exception:
        return None, "whitecheck gave no JSON (%s)" % (p.stdout or p.stderr or "")[:120]
    if p.returncode != 0 or "error" in data:
        return None, data.get("error", "whitecheck exit %d" % p.returncode)
    return data, None


def cgid_for_window(win):
    """Map a Chrome AppleScript window to a CoreGraphics window id by bounds."""
    data, err = whitecheck(["--list-chrome"], timeout=10)
    if not data:
        return None, err
    want = (win["left"], win["top"], win["right"] - win["left"], win["bottom"] - win["top"])
    best, best_d = None, 40
    for w in data.get("windows", []):
        d = (abs(w["x"] - want[0]) + abs(w["y"] - want[1]) +
             abs(w["w"] - want[2]) + abs(w["h"] - want[3]))
        if d < best_d:
            best, best_d = w, d
    if not best:
        return None, "window is not on the active desktop/Space, so it cannot be captured"
    return best["cgid"], None


def white_fraction(win):
    cgid, err = cgid_for_window(win)
    if cgid is None:
        return None, err
    data, err = whitecheck(["--window", str(cgid), "--crop-top", "90"])
    if not data:
        return None, err
    return data.get("white_frac"), None


# --------------------------------------------------------------------------
# role resolution
# --------------------------------------------------------------------------


def resolve_roles(cfg, chrome):
    """Map each role to a (window, tab_index). Self-heals when Chrome restarts."""
    wins = chrome.snapshot(cfg["url_contains"])
    cands = [w for w in wins if w["match_tabs"] and not w["minimized"]]
    by_id = {w["id"]: w for w in cands}
    order = cfg["role_order"]
    resolved, taken, changed = {}, set(), False

    for role in order:
        wid = (cfg["roles"].get(role) or {}).get("window_id")
        if wid in by_id and wid not in taken:
            resolved[role] = by_id[wid]
            taken.add(wid)

    leftovers = sorted([w for w in cands if w["id"] not in taken],
                       key=lambda w: (w["left"], w["top"]))
    for role in order:
        if role in resolved:
            continue
        if not leftovers:
            continue
        w = leftovers.pop(0)
        resolved[role] = w
        cfg["roles"].setdefault(role, {})["window_id"] = w["id"]
        changed = True
        LOG.info("role %-6s -> window %s at (%s,%s) %sx%s (newly pinned)", role, w["id"],
                 w["left"], w["top"], w["right"] - w["left"], w["bottom"] - w["top"])

    if changed and len(resolved) == len(order):
        save_config(cfg)
    elif changed:
        LOG.warning("only %d of %d roles could be pinned - not persisting, so a partial "
                    "layout cannot permanently swap %s",
                    len(resolved), len(order), "/".join(order))

    out = {}
    for role, w in resolved.items():
        tab = w["match_tabs"][0]
        if w["active_tab"] in w["match_tabs"]:
            tab = w["active_tab"]
        out[role] = (w, tab)
    return out, wins


# --------------------------------------------------------------------------
# check + recover
# --------------------------------------------------------------------------


def _blank_row(err, stalled=False):
    """A row we could not fill in. `stalled` distinguishes "the tab stopped answering"
    (recoverable - reload it) from "the window lookup failed" (not recoverable by a
    reload; needs re-discovery). Both report zero geometry, so the flag is the only
    way to tell them apart."""
    return {"left": 0, "top": 0, "right": 0, "bottom": 0, "active_tab": 0,
            "url": "", "title": "", "loading": False, "error": err, "probe": None,
            "stalled": stalled}


def probe_targets(cfg, chrome, targets, want_pixels=False):
    """Health-check every monitored tab. One osascript round-trip for all of them,
    falling back to one call each if a hung renderer stalls the batch."""
    roles = [r for r in cfg["role_order"] if r in targets]
    if not roles:
        return {}
    specs = [targets[r] for r in roles]

    probe_tmo = cfg["probe_timeout_seconds"]
    rows, stalled = None, False
    try:
        rows = chrome.batch(specs, JS_PROBE, timeout=probe_tmo)
        if len(rows) != len(specs):
            rows = None
    except OsaError as exc:
        stalled = exc.timed_out
        rows = None

    if rows is None and stalled:
        # A tab that normally answers in under half a second has stopped answering,
        # which is the symptom itself. Do NOT retry per role: recovery reloads both
        # windows regardless, so working out which one is stuck would only burn
        # another timeout each and turn a 5s detection into 15s.
        rows = [_blank_row("renderer stopped answering (probe deadline %ss/tab)" % probe_tmo,
                           stalled=True) for _ in specs]
    elif rows is None:
        rows = []
        for wid, ti in specs:
            try:
                got = chrome.batch([(wid, ti)], JS_PROBE, timeout=probe_tmo)
                rows.append(got[0] if got else _blank_row("no reply"))
            except OsaError as exc:
                rows.append(_blank_row(
                    "renderer stopped answering (probe deadline %ss/tab)" % probe_tmo
                    if exc.timed_out else exc.msg[:160], stalled=exc.timed_out))

    out = {}
    for role, (wid, ti), row in zip(roles, specs, rows):
        meta = {"url": row["url"], "title": row["title"], "loading": row["loading"]}
        if row["error"] and JS_DISABLED_MARK not in row["error"]:
            meta["probe_error"] = row["error"][:160]
        elif row["probe"] is None and not row["error"]:
            meta["probe_error"] = "probe returned nothing"

        white = None
        if want_pixels and row["active_tab"] == ti and row["right"] > row["left"]:
            white, werr = white_fraction(row)
            if werr:
                LOG.debug("pixel check unavailable for %s: %s", role, werr)

        healthy, reasons, hard = evaluate(cfg, role, meta, row["probe"], white)
        out[role] = {"role": role, "win": row, "id": wid, "tab": ti, "meta": meta,
                     "probe": row["probe"], "white": white, "healthy": healthy,
                     "reasons": reasons, "hard": hard}
    return out


def apply_scroll(cfg, chrome, role, wid, tab, target, settle_rounds=3):
    """Scroll to target, re-applying while Grafana lazily grows the page."""
    last = None
    for i in range(settle_rounds):
        try:
            res = chrome.js_json(wid, tab, js_scroll(target))
        except OsaError as exc:
            LOG.warning("scroll of %s failed: %s", role, exc.msg[:160])
            return None
        if not res or not res.get("ok"):
            LOG.warning("scroll of %s did not take: %s", role, res)
            return res
        if last and last.get("scrollHeight") == res.get("scrollHeight") and \
                abs((last.get("got") or 0) - (res.get("got") or 0)) < 4:
            return res
        last = res
        if i < settle_rounds - 1:
            time.sleep(cfg["scroll_settle_delay"])
    return last


def wait_for_render(cfg, chrome, targets, deadline):
    """Poll until every tab has panels again (or the deadline passes)."""
    specs = [targets[r] for r in cfg["role_order"] if r in targets]
    while time.time() < deadline:
        time.sleep(cfg["render_poll_seconds"])
        try:
            rows = chrome.batch(specs, JS_PROBE, timeout=cfg["probe_timeout_seconds"])
        except OsaError:
            continue
        if len(rows) == len(specs) and all(
                r["probe"] and r["probe"].get("panels", 0) >= cfg["min_panels"]
                and r["probe"].get("ready") != "loading" for r in rows):
            return True
    return False


def recover(cfg, chrome, targets, results, trigger_role, reasons, hard=False,
            good_urls=None):
    LOG.warning("RECOVERY triggered by %s: %s", trigger_role, "; ".join(reasons))
    order = [r for r in cfg["role_order"] if r in targets]

    if cfg["activate_tab_on_recovery"]:
        for role in order:
            wid, tab = targets[role]
            row = (results.get(role) or {}).get("win") or {}
            if row.get("active_tab") and row["active_tab"] != tab:
                try:
                    chrome.activate_tab(wid, tab)
                    LOG.info("%s: brought the dashboard tab (%d) to the front", role, tab)
                except OsaError as exc:
                    LOG.warning("%s: could not switch tab: %s", role, exc.msg[:120])

    for role in order:
        wid, tab = targets[role]
        try:
            if hard:
                # Never re-navigate to whatever the tab is showing now - if it is on
                # chrome-error://, that would write the error page in as the target.
                url = (results.get(role) or {}).get("meta", {}).get("url") or ""
                if cfg["url_contains"] not in url:
                    url = (good_urls or {}).get(role) or cfg.get("dashboard_url") or ""
                if url and cfg["url_contains"] in url:
                    chrome.set_url(wid, tab, url)
                    LOG.info("%s: hard re-navigated to the last known good URL", role)
                else:
                    chrome.reload(wid, tab)
                    LOG.info("%s: reloaded (no known good URL to re-navigate to)", role)
            else:
                chrome.reload(wid, tab)
                LOG.info("%s: reloaded", role)
        except OsaError as exc:
            LOG.error("%s: reload failed: %s", role, exc.msg[:160])

    if hard:
        for role in order:
            try:
                chrome.nudge(targets[role][0])
                LOG.info("%s: nudged window size to force a repaint", role)
            except OsaError:
                pass

    rendered = wait_for_render(cfg, chrome, targets, time.time() + cfg["render_wait_seconds"])
    LOG.info("panels back after reload: %s",
             "yes" if rendered else "not within %ss" % cfg["render_wait_seconds"])

    for role in order:
        wid, tab = targets[role]
        target = (cfg["roles"].get(role) or {}).get("scroll") or "top"
        res = apply_scroll(cfg, chrome, role, wid, tab, target)
        if res and res.get("ok"):
            LOG.info("%s: scrolled to %r -> %s/%s px", role, target,
                     res.get("got"), res.get("max"))


def maintain_scroll(cfg, chrome, role, wid, tab, probe):
    """Put a window back on its slice of the dashboard if Grafana reset it."""
    mode = cfg["steady_scroll"]
    if mode == "off" or not probe:
        return
    target = (cfg["roles"].get(role) or {}).get("scroll") or "top"
    if target == "top":
        return
    top, mx = probe.get("scrollTop", 0), probe.get("scrollMax", 0)
    if mx <= 0:
        return
    if target == "bottom":
        want = mx
    elif target.startswith("fraction:"):
        want = mx * float(target.split(":", 1)[1])
    elif target.startswith("px:"):
        want = float(target.split(":", 1)[1])
    else:
        want = None  # row targets: only correct a full reset

    reset = top <= cfg["scroll_reset_epsilon_px"]
    drift = want is not None and abs(top - want) > cfg["scroll_tolerance_px"]

    if mode == "always" and (drift or reset):
        pass
    elif mode == "on_reset" and reset:
        pass
    else:
        return

    res = apply_scroll(cfg, chrome, role, wid, tab, target, settle_rounds=2)
    if res and res.get("ok"):
        LOG.info("%s: re-pinned scroll to %r (was %spx) -> %s/%s",
                 role, target, top, res.get("got"), res.get("max"))


def throttled(cfg, state):
    now = time.time()
    recs = [t for t in state.get("recoveries", []) if now - t < 3600]
    state["recoveries"] = recs
    if recs and now - recs[-1] < cfg["cooldown_seconds"]:
        return "cooldown (%ds since last recovery)" % int(now - recs[-1])
    if len(recs) >= cfg["max_recoveries_per_hour"]:
        return "hit the cap of %d recoveries/hour" % cfg["max_recoveries_per_hour"]
    return None


def resolve_targets(cfg, chrome, cache):
    """{role: (window_id, tab_index)}, re-discovered only when the cache goes stale."""
    resolved, _ = resolve_roles(cfg, chrome)
    targets = {role: (w["id"], t) for role, (w, t) in resolved.items()}
    cache["targets"] = targets
    missing = [r for r in cfg["role_order"] if r not in targets]
    if missing:
        LOG.warning("no window found for role(s) %s - open the dashboard in %d window(s) "
                    "matching %r", ", ".join(missing), len(missing), cfg["url_contains"])
    return targets


def cycle(cfg, chrome, state, dry_run=False, cache=None):
    cache = {} if cache is None else cache
    want_pixels = cfg["pixel_check"] in ("on", "auto") \
        and not state.get("notes", {}).get("pixel_off")
    good_urls = state.setdefault("last_good_url", {})

    # Fast path: reuse the pinned windows and skip the full window/tab enumeration.
    targets = cache.get("targets")
    results = {}
    if targets:
        results = probe_targets(cfg, chrome, targets, want_pixels)

    # Re-discovery is for when the cache can no longer DESCRIBE reality - a role we
    # expect is absent, or a window has gone. It is emphatically NOT for a tab whose
    # URL stopped matching: that is a fault, already graded hard, and re-resolving
    # would lose it, because resolve_roles only considers windows that still have a
    # matching tab. Acting on the cached (window, tab) is what repairs it.
    vanished = [role for role, r in results.items()
                if (r["win"].get("right", 0) - r["win"].get("left", 0)) <= 0
                and not r["win"].get("stalled")]
    incomplete = [r for r in cfg["role_order"] if r not in results]
    if not targets or vanished or incomplete:
        due = time.time() - cache.get("resolved_at", 0) >= cfg["rediscover_seconds"]
        if due:
            if vanished:
                LOG.warning("window for role(s) %s is gone - re-discovering",
                            ", ".join(vanished))
            try:
                fresh = resolve_targets(cfg, chrome, cache)
                cache["resolved_at"] = time.time()
                if fresh != targets:
                    targets = fresh
                    results = probe_targets(cfg, chrome, targets, want_pixels) \
                        if targets else {}
            except OsaError as exc:
                # Chrome's browser process itself is wedged. Keep the cached targets so
                # the bad streak keeps accumulating instead of being discarded every
                # cycle, which would mean never recovering at all.
                LOG.error("cannot enumerate Chrome windows (%s) - holding the cached "
                          "windows so detection keeps accumulating", exc.msg[:140])
                cache["resolved_at"] = time.time()
        elif incomplete:
            note = state.setdefault("notes", {})
            if time.time() - note.get("incomplete_warned", 0) > 300:
                note["incomplete_warned"] = time.time()
                LOG.warning("role(s) %s have no window and are NOT being monitored",
                            ", ".join(incomplete))

    if not results:
        note = state.setdefault("notes", {})
        if time.time() - note.get("no_results_warned", 0) > 300:
            note["no_results_warned"] = time.time()
            LOG.error("no monitored windows at all - open the dashboard in %d window(s) "
                      "matching %r", len(cfg["role_order"]), cfg["url_contains"])
        save_state(state)
        return

    if chrome.js_ok is False:
        note = state.setdefault("notes", {})
        if time.time() - note.get("js_warned", 0) > 1800:
            note["js_warned"] = time.time()
            LOG.error("Chrome is not allowing JavaScript from Apple Events, so blank-page "
                      "detection and scrolling are both disabled. Fix: Chrome menu bar -> "
                      "View > Developer > Allow JavaScript from Apple Events.")

    bad_roles = []
    streak = state.setdefault("bad_streak", {})
    for role, r in results.items():
        if (r["win"].get("right", 0) - r["win"].get("left", 0)) <= 0 \
                and not r["win"].get("stalled"):
            continue  # window is gone; re-discovery handles it, a reload cannot
        if r["healthy"]:
            if streak.get(role):
                LOG.info("%s: healthy again", role)
            streak[role] = 0
            url = r["meta"].get("url") or ""
            if cfg["url_contains"] in url:
                good_urls[role] = url
            maintain_scroll(cfg, chrome, role, r["id"], r["tab"], r["probe"])
            continue
        streak[role] = streak.get(role, 0) + 1
        if r["hard"]:
            LOG.warning("%s: unhealthy - %s", role, "; ".join(r["reasons"]))
            bad_roles.append(role)
        else:
            LOG.warning("%s: unhealthy (%d/%d) - %s", role, streak[role],
                        cfg["bad_checks_before_action"], "; ".join(r["reasons"]))
            if streak[role] >= cfg["bad_checks_before_action"]:
                bad_roles.append(role)

    if not bad_roles:
        LOG.info("ok - %s", " | ".join(
            "%s: %s panels, scroll %s/%s%s%s" % (
                role, (r["probe"] or {}).get("panels", "?"),
                (r["probe"] or {}).get("scrollTop", "?"),
                (r["probe"] or {}).get("scrollMax", "?"),
                "" if not (r["probe"] or {}).get("perrs")
                else ", %d panel errors" % r["probe"]["perrs"],
                "" if r["white"] is None else ", %d%% white" % round(r["white"] * 100))
            for role, r in results.items()))
        save_state(state)
        return

    if dry_run:
        LOG.warning("dry-run: would refresh both windows now (trigger: %s)",
                    ", ".join(bad_roles))
        save_state(state)
        return

    why = throttled(cfg, state)
    if why:
        LOG.warning("holding off on recovery - %s", why)
        save_state(state)
        return

    trigger = bad_roles[0]
    escalate = streak.get(trigger, 0) >= cfg["bad_checks_before_action"] * 2
    recover(cfg, chrome, targets, results, trigger, results[trigger]["reasons"],
            hard=escalate, good_urls=good_urls)
    state.setdefault("recoveries", []).append(time.time())
    for role in results:
        streak[role] = 0
    cache.pop("targets", None)   # window ids may change after a hard re-navigation
    cache.pop("resolved_at", None)
    save_state(state)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_list(cfg, chrome, args):
    wins = chrome.snapshot(cfg["url_contains"])
    print("Chrome windows (matching %r marked *):\n" % cfg["url_contains"])
    for w in wins:
        star = "*" if w["match_tabs"] else " "
        print("%s id=%-12s (%5d,%5d) %4dx%-4d  active_tab=%-3s matching_tabs=%s%s" % (
            star, w["id"], w["left"], w["top"], w["right"] - w["left"], w["bottom"] - w["top"],
            w["active_tab"], w["match_tabs"] or "-", "  [minimized]" if w["minimized"] else ""))
    print()
    for role in cfg["role_order"]:
        print("  role %-6s pinned to window_id=%s, scroll=%r" % (
            role, (cfg["roles"].get(role) or {}).get("window_id"),
            (cfg["roles"].get(role) or {}).get("scroll")))


def cmd_pin(cfg, chrome, args):
    if len(args) == 2:
        for role, wid in zip(cfg["role_order"], args):
            cfg["roles"].setdefault(role, {})["window_id"] = int(wid)
        save_config(cfg)
        print("pinned %s" % ", ".join("%s=%s" % (r, w) for r, w in zip(cfg["role_order"], args)))
        return
    for role in cfg["role_order"]:
        cfg["roles"].setdefault(role, {})["window_id"] = None
    save_config(cfg)
    targets, _ = resolve_roles(cfg, chrome)
    if not targets:
        print("No Chrome window has a tab matching %r." % cfg["url_contains"])
        return
    for role in cfg["role_order"]:
        if role in targets:
            w, t = targets[role]
            print("  %-6s -> window %s at (%s,%s), tab %s" % (role, w["id"], w["left"], w["top"], t))


def cmd_probe(cfg, chrome, args):
    targets = resolve_targets(cfg, chrome, {})
    results = probe_targets(cfg, chrome, targets, cfg["pixel_check"] in ("on", "auto"))
    for role in cfg["role_order"]:
        if role not in results:
            print("%s: no window" % role)
            continue
        r = results[role]
        print("=== %s (window %s, tab %s) %s" % (
            role, r["id"], r["tab"], "HEALTHY" if r["healthy"] else "UNHEALTHY"))
        print("    url    : %s" % r["meta"].get("url", "")[:110])
        print("    title  : %s" % r["meta"].get("title", "")[:110])
        if r["meta"].get("probe_error"):
            print("    probe  : %s" % r["meta"]["probe_error"])
        if r["probe"]:
            pr = r["probe"]
            print("    panels=%s charts=%s text=%s panelErrors=%s hidden=%s" % (
                pr.get("panels"), pr.get("charts"), pr.get("text"),
                pr.get("perrs"), pr.get("hidden")))
            print("    scroller=%s scrollTop=%s scrollMax=%s" % (
                pr.get("scroller"), pr.get("scrollTop"), pr.get("scrollMax")))
            print("    rows   : %s" % ", ".join(pr.get("rows", [])[:12]))
        if r["white"] is not None:
            print("    white  : %d%%" % round(r["white"] * 100))
        for why in r["reasons"]:
            print("    !! %s" % why)
        if not r["healthy"]:
            print("    action : %s" % ("immediately" if r["hard"]
                                       else "after %d checks in a row"
                                       % cfg["bad_checks_before_action"]))


def cmd_scroll(cfg, chrome, args):
    """scroll <role> [target] - apply a scroll target now."""
    if not args:
        print("usage: watchgrafana scroll <role> [top|bottom|fraction:0.5|px:1200|row:TEXT]")
        return
    role = args[0]
    target = args[1] if len(args) > 1 else (cfg["roles"].get(role) or {}).get("scroll") or "top"
    targets = resolve_targets(cfg, chrome, {})
    if role not in targets:
        print("no window pinned for role %r" % role)
        return
    wid, tab = targets[role]
    print(json.dumps(apply_scroll(cfg, chrome, role, wid, tab, target), indent=2))


def cmd_doctor(cfg, chrome, args):
    ok = True
    print("watchgrafana doctor\n" + "-" * 60)
    print("config      : %s" % CONFIG_PATH)
    print("log         : %s" % os.path.join(HERE, cfg["log_file"]))
    print("match URL   : %r" % cfg["url_contains"])
    bad = tcc_protected(HERE)
    if bad:
        print("[warn] installed under ~/%s, which macOS protects — the launchd agent cannot" % bad)
        print("       read it there and will die on every respawn. Move it out of ~/%s" % bad)
        print("       and re-run install. Interactive commands are unaffected.")

    try:
        wins = chrome.snapshot(cfg["url_contains"])
        print("[ok]   Chrome automation works (%d windows)" % len(wins))
    except OsaError as exc:
        print("[FAIL] cannot talk to Chrome: %s" % exc.msg[:200])
        print("       Grant it in System Settings > Privacy & Security > Automation.")
        return

    matching = [w for w in wins if w["match_tabs"]]
    if len(matching) >= 2:
        print("[ok]   %d windows have the dashboard open" % len(matching))
    else:
        ok = False
        print("[warn] only %d window(s) have a tab matching %r — need 2" % (len(matching), cfg["url_contains"]))

    targets, _ = resolve_roles(cfg, chrome)
    for role in cfg["role_order"]:
        if role in targets:
            w, t = targets[role]
            print("       %-6s = window %s at (%s,%s) %sx%s, tab %s, scroll=%r" % (
                role, w["id"], w["left"], w["top"], w["right"] - w["left"], w["bottom"] - w["top"],
                t, (cfg["roles"].get(role) or {}).get("scroll")))
        else:
            print("       %-6s = (unassigned)" % role)

    if targets:
        role = cfg["role_order"][0] if cfg["role_order"][0] in targets else list(targets)[0]
        win, tab = targets[role]
        try:
            chrome.js(win["id"], tab, "1+1")
            print("[ok]   Chrome runs JavaScript from Apple Events")
        except OsaError as exc:
            ok = False
            if JS_DISABLED_MARK in exc.msg:
                print("[FAIL] Chrome blocks JavaScript from Apple Events — blank-page detection and")
                print("       scrolling need it. Fix (one click, no restart):")
                print("         Chrome menu bar > View > Developer > Allow JavaScript from Apple Events")
                print("       ...or let the tool click it for you:  ./watchgrafana.py enable-js")
            else:
                print("[FAIL] JavaScript probe failed: %s" % exc.msg[:200])

    if cfg["pixel_check"] == "off":
        print("[skip] pixel white-check disabled (config pixel_check = \"off\")")
    else:
        data, err = whitecheck(["--list-chrome"], timeout=10)
        if err:
            print("[warn] whitecheck helper: %s" % err)
        elif targets:
            role = list(targets)[0]
            w, _ = targets[role]
            frac, err = white_fraction(w)
            if err:
                print("[warn] pixel check unavailable: %s" % err)
                print("       Enable System Settings > Privacy & Security > Screen Recording for the")
                print("       app that runs this script, then re-run doctor.")
            else:
                print("[ok]   pixel check works (%s is %d%% white right now)" % (role, round(frac * 100)))

    print("-" * 60)
    print("doctor: %s" % ("all good" if ok else "see the FAIL/warn lines above"))


PLIST_LABEL = "me.junchen.watchgrafana"


def plist_path():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % PLIST_LABEL)


PROTECTED_DIRS = ("Downloads", "Desktop", "Documents")


def tcc_protected(path):
    """macOS shields these folders. A launchd agent has no grant for them, so it
    cannot even read its own script there — Python exits 2 before logging a thing."""
    home = os.path.expanduser("~")
    rel = os.path.relpath(os.path.abspath(path), home)
    if rel.startswith(".."):
        return None
    top = rel.split(os.sep)[0]
    return top if top in PROTECTED_DIRS else None


def cmd_install(cfg, chrome, args):
    bad = tcc_protected(HERE)
    if bad and "--force" not in args:
        print("[FAIL] This tool lives in ~/%s, which macOS protects." % bad)
        print()
        print("       Your terminal can read it, but the launchd agent runs as a different")
        print("       process with no access there, so it would die with exit code 2 on every")
        print("       respawn without ever writing a log line.")
        print()
        print("       Move it somewhere unprotected first, then install from there:")
        print("         mv %s ~/%s" % (HERE, os.path.basename(HERE)))
        print("         cd ~/%s && ./watchgrafana.py install" % os.path.basename(HERE))
        print()
        print("       (./watchgrafana.py install --force installs anyway, if you have granted")
        print("        Full Disk Access to /usr/bin/python3 yourself.)")
        return
    script = os.path.join(HERE, "watchgrafana.py")
    body = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>%s</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>WorkingDirectory</key><string>%s</string>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
""" % (PLIST_LABEL, script, HERE,
       os.path.join(HERE, "logs", "launchd.out.log"),
       os.path.join(HERE, "logs", "launchd.err.log"))
    os.makedirs(os.path.dirname(plist_path()), exist_ok=True)
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    with open(plist_path(), "w") as fh:
        fh.write(body)
    uid = os.getuid()
    subprocess.run(["/bin/launchctl", "bootout", "gui/%d/%s" % (uid, PLIST_LABEL)],
                   capture_output=True)
    p = subprocess.run(["/bin/launchctl", "bootstrap", "gui/%d" % uid, plist_path()],
                       capture_output=True, text=True)
    if p.returncode != 0:
        p = subprocess.run(["/bin/launchctl", "load", "-w", plist_path()],
                           capture_output=True, text=True)
    print("installed %s" % plist_path())
    print("launchctl: %s" % (p.stderr.strip() or "loaded"))
    print("watchdog now starts at login and restarts if it dies.")


def cmd_uninstall(cfg, chrome, args):
    uid = os.getuid()
    subprocess.run(["/bin/launchctl", "bootout", "gui/%d/%s" % (uid, PLIST_LABEL)],
                   capture_output=True)
    subprocess.run(["/bin/launchctl", "unload", plist_path()], capture_output=True)
    if os.path.exists(plist_path()):
        os.remove(plist_path())
    print("removed %s" % plist_path())


def cmd_stop(cfg, chrome, args):
    """Stop the background agent, keeping it installed so `start` brings it back."""
    if not os.path.exists(plist_path()):
        print("not installed — nothing to stop (run: watchgrafana install)")
        return
    p = subprocess.run(["/bin/launchctl", "bootout", "gui/%d/%s" % (os.getuid(), PLIST_LABEL)],
                       capture_output=True, text=True)
    err = (p.stderr or "").strip()
    if p.returncode != 0 and "No such process" not in err:
        print("could not stop it: %s" % (err or "launchctl exit %d" % p.returncode))
        return
    print("watchdog stopped. It stays installed — `watchgrafana start` brings it back,")
    print("and it would also come back by itself at your next login.")
    print("To stop it for good instead: watchgrafana uninstall")


def cmd_start(cfg, chrome, args):
    """Start the background agent again after `stop`."""
    if not os.path.exists(plist_path()):
        print("not installed yet — run: watchgrafana install")
        return
    uid = os.getuid()
    if subprocess.run(["/bin/launchctl", "print", "gui/%d/%s" % (uid, PLIST_LABEL)],
                      capture_output=True).returncode == 0:
        print("already running — watchgrafana status")
        return
    p = subprocess.run(["/bin/launchctl", "bootstrap", "gui/%d" % uid, plist_path()],
                       capture_output=True, text=True)
    if p.returncode != 0:
        print("could not start it: %s" % (p.stderr or "").strip())
        return
    print("watchdog started. Watch it with:  tail -f %s"
          % os.path.join(HERE, cfg["log_file"]))


def cmd_status(cfg, chrome, args):
    uid = os.getuid()
    p = subprocess.run(["/bin/launchctl", "print", "gui/%d/%s" % (uid, PLIST_LABEL)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        print("watchdog is not loaded in launchd (run: watchgrafana install)")
    else:
        for line in p.stdout.splitlines():
            if any(k in line for k in ("state =", "pid =", "last exit", "runs =")):
                print(line.strip())
    st = load_state()
    recs = st.get("recoveries", [])
    print("recoveries in the last hour: %d" % len([t for t in recs if time.time() - t < 3600]))
    if recs:
        print("last recovery: %s" % datetime.fromtimestamp(recs[-1]).strftime("%Y-%m-%d %H:%M:%S"))


def cmd_test(cfg, chrome, args):
    """Run the full recovery path once, as if a window had gone white."""
    targets = resolve_targets(cfg, chrome, {})
    if not targets:
        print("no windows resolved")
        return
    results = probe_targets(cfg, chrome, targets)
    trigger = cfg["role_order"][0] if cfg["role_order"][0] in targets else list(targets)[0]
    recover(cfg, chrome, targets, results, trigger, ["forced by `watchgrafana test`"])


NO_AX_MARKS = ("assistive access", "-25211", "-1719", "not authorized",
               "not allowed", "1002")


def js_works(chrome, targets):
    for role, (wid, tab) in targets.items():
        try:
            chrome.js(wid, tab, "1+1")
            return True, None
        except OsaError as exc:
            return False, exc
    return False, None


def cmd_enable_js(cfg, chrome, args):
    """Tick Chrome's View > Developer > Allow JavaScript from Apple Events for us."""
    targets = resolve_targets(cfg, chrome, {})
    if not targets:
        print("No Chrome window has the dashboard open, so there is nothing to verify against.")
        return

    ok, exc = js_works(chrome, targets)
    if ok:
        print("[ok] Chrome already allows JavaScript from Apple Events - nothing to do.")
        return
    if exc and JS_DISABLED_MARK not in exc.msg:
        print("[FAIL] JavaScript is failing for a different reason:\n       %s" % exc.msg[:300])
        return

    print("Clicking View > Developer > Allow JavaScript from Apple Events ...")
    try:
        res = osa(ENABLE_JS_SCPT, cfg["browser_app"], timeout=30)
    except OsaError as exc:
        low = exc.msg.lower()
        if any(m in low for m in NO_AX_MARKS):
            print("[FAIL] This terminal is not allowed to control other apps' menus yet.")
            print()
            print("       Grant it once, then re-run this command:")
            print("         System Settings > Privacy & Security > Accessibility")
            print("         > turn ON the app you are running this from (Terminal / iTerm)")
        else:
            print("[FAIL] could not drive the menu: %s" % exc.msg[:300])
        return

    if res.startswith("NOTFOUND"):
        print("[FAIL] no 'Apple Events' item in Chrome's Developer menu. Items found:")
        print("       %s" % res.split(":", 1)[1].strip()[:400])
        return

    print("       %s" % res)
    ok, exc = js_works(chrome, targets)
    if ok:
        print("[ok] Chrome now runs JavaScript from Apple Events.")
        print("     The running watchdog picks this up on its next cycle.")
        return
    print("[FAIL] The click reported success but the setting did not change.")
    print("       Some machines ignore synthetic clicks on this item entirely.")
    print("       Use ./enable-js-restart.py instead - it writes the pref directly.")


def cmd_run(cfg, chrome, args):
    LOG.info("watchgrafana started (interval %ss, roles %s, pixel_check=%s)",
             cfg["interval_seconds"], cfg["role_order"], cfg["pixel_check"])
    state = load_state()
    cache = {}
    while True:
        started = time.time()
        try:
            cycle(cfg, chrome, state, cache=cache)
        except OsaError as exc:
            LOG.error("cycle failed talking to Chrome: %s", exc.msg[:200])
            cache.pop("targets", None)
        except Exception as exc:  # never let the watchdog die
            LOG.exception("cycle raised: %s", exc)
            cache.pop("targets", None)
        time.sleep(max(1.0, cfg["interval_seconds"] - (time.time() - started)))


def cmd_once(cfg, chrome, args):
    state = load_state()
    cycle(cfg, chrome, state, dry_run="--dry-run" in args, cache={})


COMMANDS = {
    "doctor": cmd_doctor, "list": cmd_list, "pin": cmd_pin, "probe": cmd_probe,
    "enable-js": cmd_enable_js,
    "scroll": cmd_scroll, "once": cmd_once, "run": cmd_run, "test": cmd_test,
    "install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status,
    "start": cmd_start, "stop": cmd_stop,
}

USAGE = """watchgrafana — keep two Chrome windows pinned on a Grafana dashboard

  doctor              check permissions, windows and roles (start here)
  enable-js           tick Chrome's "Allow JavaScript from Apple Events" for you
  list                list Chrome windows and which ones match
  pin [id id]         re-pin roles (no args = auto-pin left-to-right)
  probe               one detailed health report per role
  scroll <role> [t]   apply a scroll target now (top|bottom|fraction:F|px:N|row:TEXT)
  once [--dry-run]    run a single check cycle
  run                 watch forever (this is what launchd runs)
  test                force the recovery path: reload both + re-scroll
  install/uninstall   add/remove the launchd agent (auto-start at login)
  start / stop        start or pause the background agent without uninstalling
  status              is the agent running, and recent recovery count
"""


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd = argv[0]
    if cmd not in COMMANDS:
        print("unknown command %r\n" % cmd)
        print(USAGE)
        return 2
    cfg = load_config()
    setup_logging(cfg, to_console=(cmd != "run"))
    chrome = Chrome(cfg)
    COMMANDS[cmd](cfg, chrome, argv[1:])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
