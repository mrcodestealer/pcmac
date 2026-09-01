#!/bin/bash
# clickthis.command — double-click in Finder to bring the dashboard wall back up.
#
#   1. re-opens the saved Chrome windows at their saved positions (launching Chrome
#      if it is not running)
#   2. waits until the dashboard windows are actually there
#   3. starts the background watchdog (installing it the first time)
#   4. shows you the state it ended in
#
# Safe to run when things are already fine: re-opening only creates windows that are
# missing, and starting an already-running watchdog is a no-op.
#
#   ./clickthis.command --dry-run    say what it would do, change nothing

cd "$(dirname "$0")" || exit 1
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="1"

PY=/usr/bin/python3
LABEL=me.junchen.watchgrafana
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()  { printf '   \033[32mok\033[0m   %s\n' "$*"; }
bad() { printf '   \033[31mFAIL\033[0m %s\n' "$*"; }
note(){ printf '        %s\n' "$*"; }

matching_windows() {
  "$PY" watchgrafana.py list 2>/dev/null | grep -c '^\*'
}

say "Grafana dashboard wall — starting up"
printf '   %s\n' "$(pwd)"

# ---------------------------------------------------------------- 1. windows
say "1. Chrome windows"
if [ ! -s layout.json ]; then
  bad "layout.json is missing, so I do not know where your windows go."
  note "Open the dashboard in two windows, arrange them, then run:"
  note "    ./enable-js-restart.py          (saves the layout for next time)"
else
  have=$(matching_windows)
  note "dashboard windows open right now: $have"
  if [ -n "$DRY" ]; then
    note "dry-run: would run ./enable-js-restart.py --restore-only layout.json"
  else
    "$PY" enable-js-restart.py --restore-only layout.json 2>&1 | sed 's/^/        /'
  fi
fi

# ---------------------------------------------------------------- 2. settle
say "2. Waiting for the dashboard windows"
# each check is a full Chrome window/tab scan (~2s), so this is the wait, not the sleep
found=$(matching_windows)
if [ -z "$DRY" ]; then
  for _ in $(seq 1 15); do
    [ "$found" -ge 2 ] && break
    sleep 1
    found=$(matching_windows)
  done
fi
if [ "$found" -ge 2 ]; then
  ok "$found windows have the dashboard open"
else
  bad "only $found window(s) have the dashboard open — the watchdog needs 2"
  note "Open the dashboard in a second window and put it on the other screen;"
  note "the watchdog pins whichever is leftmost as 'top'."
fi

# ---------------------------------------------------------------- 3. watchdog
say "3. Watchdog"
if [ -n "$DRY" ]; then
  if [ -f "$PLIST" ]; then note "dry-run: would run ./watchgrafana.py start"
  else note "dry-run: would run ./watchgrafana.py install"; fi
elif [ -f "$PLIST" ]; then
  "$PY" watchgrafana.py start 2>&1 | sed 's/^/        /'
else
  note "not installed yet — installing so it also starts at every login"
  "$PY" watchgrafana.py install 2>&1 | sed 's/^/        /'
fi

# ---------------------------------------------------------------- 4. report
say "4. Where things stand"
"$PY" watchgrafana.py status 2>&1 | sed 's/^/        /'
"$PY" watchgrafana.py doctor 2>&1 | grep -E '^\[|^doctor:' | sed 's/^/        /'

if [ -z "$DRY" ]; then
  say "Latest watchdog activity"
  sleep 6
  tail -4 logs/watchgrafana.log 2>/dev/null | sed 's/^/        /'
fi

printf '\n'
if [ -t 0 ]; then
  printf 'Press any key to close this window.'
  read -r -n 1 -s
  printf '\n'
fi
