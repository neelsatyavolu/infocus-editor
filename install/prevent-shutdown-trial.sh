#!/bin/bash
# Runs as root on the editing Mac. Installs the shutdown clearer from this
# same directory, proves a shutdown a few minutes out is removed, then waits
# past that minute. The portal script scripts/editor-prevent-shutdown.mjs
# is what asks for the admin password and starts this.
set -euo pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin
[ "$(id -u)" -eq 0 ] || { echo "not-root"; exit 1; }

SRC=$(cd "$(dirname "$0")" && pwd)
BIN=/Library/InFocusEditor/bin/prevent-shutdown
PLIST=/Library/LaunchDaemons/com.infocus.editor.prevent-shutdown.plist
LABEL=com.infocus.editor.prevent-shutdown

restore_wake() {
  /usr/bin/pmset repeat cancel || true
  /usr/bin/pmset repeat wakeorpoweron MTWRF 07:00:00 || true
  /bin/launchctl bootstrap system "$PLIST" 2>/dev/null || true
}

install -o root -g wheel -m 755 "$SRC/prevent-shutdown.sh" "$BIN"
install -o root -g wheel -m 644 "$SRC/com.infocus.editor.prevent-shutdown.plist" "$PLIST"
launchctl bootout system/"$LABEL" 2>/dev/null || true
launchctl bootstrap system "$PLIST"
boot1=$(sysctl -n kern.boottime)
echo "BOOT_BEFORE $boot1"
echo "=== INSTALLED ==="
"$BIN" || true
pmset -g sched

launchctl bootout system/"$LABEL"
when=$(date -v+3M "+%H:%M:00")
echo "=== ARM $when ==="
if ! /usr/bin/pmset repeat shutdown MTWRFSU "$when" wakeorpoweron MTWRF 07:00:00; then
  echo "FAIL: could not arm a test shutdown"
  restore_wake
  exit 1
fi
echo "=== ARMED ==="
pmset -g sched
if ! pmset -g sched | grep -q 'shutdown'; then
  echo "FAIL: test shutdown did not arm"
  restore_wake
  exit 1
fi
launchctl bootstrap system "$PLIST"
"$BIN" || true
sleep 2
echo "=== CLEARED ==="
pmset -g sched
if pmset -g sched | grep -q 'shutdown'; then
  echo "FAIL: shutdown still scheduled"
  restore_wake
  exit 1
fi

target=$(date -j -f "%Y-%m-%d %H:%M:%S" "$(date +%Y-%m-%d) $when" +%s)
now=$(date +%s)
wait=$((target - now + 30))
[ "$wait" -lt 0 ] && wait=0
[ "$wait" -gt 240 ] && wait=240
echo "=== WAIT ${wait}s past $when ==="
sleep "$wait"
boot2=$(sysctl -n kern.boottime)
echo "=== AFTER $(date) ==="
echo "BOOT_AFTER $boot2"
pmset -g sched
if [ "$boot1" != "$boot2" ]; then
  echo "FAIL: the Mac restarted"
  exit 1
fi
if pmset -g sched | grep -q 'shutdown'; then
  echo "FAIL: shutdown came back"
  "$BIN" || true
  exit 1
fi
launchctl bootstrap system "$PLIST" 2>/dev/null || true
echo "PASS"
