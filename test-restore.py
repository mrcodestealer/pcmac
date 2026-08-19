import importlib.util, json, os, shutil, sys, tempfile
spec = importlib.util.spec_from_file_location('ejr', 'enable-js-restart.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
fails = []
def check(name, ok, detail=""):
    print("%-6s %-46s %s" % ("PASS" if ok else "**FAIL**", name, detail[:64]))
    if not ok: fails.append(name)

DASH  = "https://grafana.client8.me/d/281e8816/core-metrics-arms-aliyun?orgId=1"
OTHER = "https://ecs.console.alibabacloud.com/"

def run(saved, after_relaunch, final=None):
    calls = []
    snaps = [after_relaunch, final if final is not None else after_relaunch]
    m.snapshot = lambda: snaps.pop(0) if snaps else []
    def fake_osa(script, *args, **kw):
        name = {id(m.MAKE_WINDOW): "MAKE", id(m.ADD_TAB): "ADDTAB",
                id(m.SET_BOUNDS): "BOUNDS", id(m.ACTIVATE_TAB): "ACTTAB",
                id(m.CLOSE_WINDOW): "CLOSE"}.get(id(script), "?")
        calls.append((name, args)); return "99" if name == "MAKE" else "ok"
    m.osa = fake_osa
    m.time.sleep = lambda *_: None
    m.restore(saved)
    return calls

print("--- case A: two windows on the SAME dashboard, Chrome restored one ---")
saved = [{"id": 1, "bounds": [7, 25, 963, 1080], "active_tab": 1, "tabs": [DASH, OTHER]},
         {"id": 2, "bounds": [964, 25, 1920, 1080], "active_tab": 1, "tabs": [DASH]}]
after = [{"id": 9, "bounds": [0, 0, 800, 600], "active_tab": 1, "tabs": [DASH]},
         {"id": 10, "bounds": [0, 0, 800, 600], "active_tab": 1, "tabs": ["chrome://newtab/"]}]
final = after + [{"id": 99, "bounds": [964, 25, 1920, 1080], "active_tab": 1, "tabs": [DASH]}]
c = run(saved, after, final)
k = [x[0] for x in c]
check("both dashboard windows end up present", k.count("MAKE") == 1, str(k))
check("the missing one is re-created at its bounds",
      [x for x in c if x[0] == "MAKE"][0][1][1:] == (964, 25, 1920, 1080),
      str([x for x in c if x[0] == "MAKE"][0][1][1:]))
check("the extra tab is added to the survivor", k.count("ADDTAB") == 1, str(k))
check("survivor's geometry is re-applied", k.count("BOUNDS") == 1, str(k))
check("empty New Tab window is closed", k.count("CLOSE") == 1, str(k))

print("\n--- case B: Chrome restored nothing ---")
c = run(saved, [{"id": 10, "bounds": [0, 0, 800, 600], "active_tab": 1, "tabs": ["chrome://newtab/"]}],
        [{"id": 10, "bounds": [0, 0, 800, 600], "active_tab": 1, "tabs": ["chrome://newtab/"]}])
k = [x[0] for x in c]
check("both windows re-created from scratch", k.count("MAKE") == 2, str(k))
check("the 2-tab window gets its second tab", k.count("ADDTAB") == 1, str(k))
check("New Tab window still closed", k.count("CLOSE") == 1, str(k))

print("\n--- case C: Chrome restored everything ---")
after = [{"id": 9, "bounds": [0, 0, 800, 600], "active_tab": 1, "tabs": [DASH, OTHER]},
         {"id": 8, "bounds": [0, 0, 800, 600], "active_tab": 1, "tabs": [DASH]}]
c = run(saved, after, after)
k = [x[0] for x in c]
check("nothing is re-created", k.count("MAKE") == 0, str(k))
check("no tabs are duplicated", k.count("ADDTAB") == 0, str(k))
check("both windows get their geometry back", k.count("BOUNDS") == 2, str(k))
check("no window is closed", k.count("CLOSE") == 0, str(k))

print("\n%s" % ("all checks passed" if not fails else "%d FAILED: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
