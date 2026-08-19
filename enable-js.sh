#!/bin/bash
# enable-js.sh — tick Chrome's  View > Developer > Allow JavaScript from Apple Events.
#
# Standalone: needs nothing but Chrome and this file. Safe to re-run any time —
# it checks the setting first and does nothing if it is already on. Run it again
# after a Chrome update, which resets the setting.
#
# Requires this terminal to have Accessibility permission:
#   System Settings > Privacy & Security > Accessibility > turn ON Terminal
#
#   ./enable-js.sh            # Google Chrome
#   ./enable-js.sh "Brave Browser"

set -u
APP="${1:-Google Chrome}"

probe_js() {
  /usr/bin/osascript - "$APP" <<'EOF' 2>&1
on run argv
  set appName to item 1 of argv
  using terms from application "Google Chrome"
    tell application appName
      return (execute (active tab of front window) javascript "1+1") as text
    end tell
  end using terms from
end run
EOF
}

click_menu_item() {
  /usr/bin/osascript - "$APP" <<'EOF' 2>&1
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
EOF
}

out="$(probe_js)"
if [ "$out" = "2" ]; then
  echo "[ok] $APP already allows JavaScript from Apple Events — nothing to do."
  exit 0
fi
case "$out" in
  *"turned off"*) ;;
  *)
    echo "[FAIL] JavaScript is failing for a different reason:"
    echo "       $out"
    exit 1 ;;
esac

echo "clicking  View > Developer > Allow JavaScript from Apple Events ..."
click="$(click_menu_item)"
echo "       $click"

case "$click" in
  *"assistive access"*|*-25211*|*-1719*|*"not authorized"*|*"not allowed"*)
    echo
    echo "[FAIL] This terminal may not control other apps' menus yet."
    echo "       Grant it once, then re-run this script:"
    echo "         System Settings > Privacy & Security > Accessibility"
    echo "         > turn ON the app you are running this from (Terminal / iTerm)"
    echo "       macOS may have just shown you that prompt."
    exit 1 ;;
  NOTFOUND*)
    echo
    echo "[FAIL] No 'Apple Events' entry in Chrome's Developer menu (items listed above)."
    echo "       Set it by hand: View > Developer > Allow JavaScript from Apple Events"
    exit 1 ;;
esac

out="$(probe_js)"
if [ "$out" = "2" ]; then
  echo "[ok] $APP now runs JavaScript from Apple Events."
  echo "     A running watchdog picks this up on its next cycle — nothing to restart."
  exit 0
fi
echo "[FAIL] The click landed but the setting is still off — it was probably ON and just"
echo "       got switched OFF. Run this script once more to toggle it back."
exit 1
