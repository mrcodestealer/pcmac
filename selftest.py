#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""selftest — verify watchgrafana's logic without touching the live dashboards.

Part 1 runs the injected JavaScript inside a throwaway headless Chrome against a
page shaped like a Grafana dashboard (inner scroll container, panel testids, row
titles) and checks every scroll mode lands where it should.
Part 2 feeds synthetic probe results to the health evaluator.

    ./selftest.py
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import watchgrafana as wg  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
fails = []


def check(name, ok, detail=""):
    print("%-6s %-38s %s" % ("PASS" if ok else "**FAIL**", name, detail[:80]))
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------- part 1: JS


def build_mock(path):
    rows = ["KPI", "CPMS1.0 / CPMS2.0", "FPMS2.0", "Pulsar 投注"]
    panels = "".join(
        '<div class="dashboard-row"><div class="dashboard-row__title">%s</div></div>' % r +
        "".join('<div data-panelid="%d"><div data-testid="data-testid panel content">'
                '<canvas width="200" height="120"></canvas>panel %d body text</div></div>'
                % (i * 10 + j, i * 10 + j) for j in range(4))
        for i, r in enumerate(rows))

    cases = [
        ("probe", wg.JS_PROBE),
        ("scroll_bottom", wg.js_scroll("bottom")),
        ("scroll_top", wg.js_scroll("top")),
        ("scroll_frac", wg.js_scroll("fraction:0.5")),
        ("scroll_px", wg.js_scroll("px:400")),
        ("scroll_row_hit", wg.js_scroll("row:Pulsar")),
        ("scroll_row_miss", wg.js_scroll("row:NoSuchRow")),
    ]
    script = "\n".join(
        "  try { out.push([%s, JSON.parse(eval(%s))]); }"
        " catch (e) { out.push([%s, {threw: String(e)}]); }"
        % (json.dumps(n), json.dumps(js), json.dumps(n)) for n, js in cases)

    page = """<!doctype html><meta charset="utf-8"><title>mock</title>
<style>
 html,body{margin:0;height:100%%;overflow:hidden;background:#111217;color:#ccc}
 .main-view{height:100%%;display:flex;flex-direction:column}
 .scrollbar-view{flex:1;overflow-y:auto}
 [data-panelid]{height:220px;border:1px solid #333;margin:4px}
 .dashboard-row__title{font:16px sans-serif;padding:8px}
</style>
<div class="main-view"><div class="scrollbar-view"><div class="dashboard-container">%s</div></div></div>
<pre id="out"></pre>
<script>
window.addEventListener('load', function(){
  var out = [];
%s
  document.getElementById('out').textContent = 'RESULTS:' + JSON.stringify(out);
});
</script>
""" % (panels, script)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)


def test_js():
    print("\n--- injected JavaScript, in a throwaway headless Chrome ---")
    if not os.path.exists(CHROME):
        check("headless Chrome available", False, CHROME + " not found")
        return
    tmp = tempfile.mkdtemp(prefix="wg-selftest-")
    mock = os.path.join(tmp, "mock.html")
    build_mock(mock)
    cmd = ["/usr/bin/perl", "-e", "alarm 60; exec @ARGV", CHROME,
           "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
           "--user-data-dir=" + os.path.join(tmp, "profile"),
           "--window-size=1200,800", "--dump-dom", "file://" + mock]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        check("headless Chrome ran", False, "timed out")
        return
    dom = (p.stdout or "").replace("\n", "")
    if "RESULTS:" not in dom:
        check("headless Chrome ran", False, "no results in DOM dump")
        return
    blob = dom.split("RESULTS:", 1)[1].split("</pre>", 1)[0]
    blob = blob.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")
    res = dict(json.loads(blob))

    pr = res.get("probe", {})
    check("probe finds Grafana's inner scroller", pr.get("scroller") == "DIV.scrollbar-view",
          str(pr.get("scroller")))
    check("probe counts panels (deduped)", pr.get("panels") == 16, "panels=%s" % pr.get("panels"))
    check("probe counts rendered charts", pr.get("charts", 0) >= 16, "charts=%s" % pr.get("charts"))
    check("probe reads row titles (incl. CJK)",
          pr.get("rows") == ["KPI", "CPMS1.0 / CPMS2.0", "FPMS2.0", "Pulsar 投注"],
          str(pr.get("rows")))
    check("probe sees a scrollable page", pr.get("scrollMax", 0) > 1000,
          "scrollMax=%s" % pr.get("scrollMax"))

    mx = pr.get("scrollMax", 0)
    for name, want in (("scroll_bottom", mx), ("scroll_top", 0),
                       ("scroll_frac", mx // 2), ("scroll_px", 400),
                       ("scroll_row_miss", mx)):
        r = res.get(name, {})
        check(name, r.get("ok") and abs(r.get("got", -1) - want) <= 2,
              "got=%s want=%s" % (r.get("got"), want))
    rh = res.get("scroll_row_hit", {})
    check("scroll_row_hit lands on the named row",
          rh.get("how") == "row-hit" and rh.get("got", 0) > mx * 0.5,
          "how=%s got=%s" % (rh.get("how"), rh.get("got")))


# ----------------------------------------------------------- part 2: verdicts

GOOD = {"ok": True, "text": 4000, "panels": 24, "charts": 30, "login": False,
        "appErr": 0, "hidden": False, "scrollTop": 0, "scrollMax": 3000}
URL = "https://grafana.client8.me/d/281e8816/core-metrics-arms-aliyun?orgId=1"
META = {"url": URL, "title": "Core Metrics Arms - Aliyun", "loading": False}


def test_verdicts():
    print("\n--- health verdicts ---")
    cfg = wg.load_config()

    def ev(name, expect, meta=None, probe="good", white=None, hard=None):
        m = dict(META)
        m.update(meta or {})
        if probe == "good":
            p = dict(GOOD)
        elif probe is None:
            p = None
        else:
            p = dict(GOOD)
            p.update(probe)
        healthy, reasons, is_hard = wg.evaluate(cfg, "top", m, p, white)
        ok = healthy == expect and (hard is None or is_hard == hard)
        check(name, ok, ("healthy" if healthy
                         else "%s: %s" % ("IMMEDIATE" if is_hard else "needs 2 checks",
                                          "; ".join(reasons))))

    ev("normal dashboard", True)
    # unambiguous -> act on the very first check
    ev("blank white page", False, probe={"text": 0, "panels": 0, "charts": 0}, hard=True)
    ev("bounced to Grafana login", False, probe={"login": True}, hard=True)
    ev("Grafana error screen", False, probe={"appErr": 1}, hard=True)
    ev("Aw-Snap crash page", False, meta={"title": "Aw, Snap!"}, probe=None, hard=True)
    ev("chrome-error:// url", False, meta={"url": "chrome-error://chromewebdata/"},
       probe=None, hard=True)
    ev("tab navigated elsewhere", False,
       meta={"url": "https://ecs.console.alibabacloud.com/"}, probe=None, hard=True)
    # ambiguous -> confirm before acting
    ev("panels present, nothing painted", False, probe={"charts": 0}, hard=False)
    ev("renderer hung, no probe answer", False,
       meta={"probe_error": "renderer did not answer within 20s"}, probe=None, hard=False)
    ev("screen is 95% white", False, white=0.95, hard=False)
    # must not trip at all
    ev("background tab, nothing painted", True, probe={"charts": 0, "hidden": True})
    ev("JS-from-AppleEvents off (must NOT trip)", True, probe=None)
    ev("screen is 30% white (normal dark theme)", True, white=0.30)
    ev("half-loaded page still has text", False, probe={"panels": 0}, hard=False)


if __name__ == "__main__":
    test_js()
    test_verdicts()
    print("\n%s" % ("all checks passed" if not fails
                    else "%d FAILED: %s" % (len(fails), ", ".join(fails))))
    sys.exit(1 if fails else 0)
