#!/bin/bash
# Remove a shutdown from the macOS power schedule. Keep the weekday 7:00 AM
# power-on. A LaunchDaemon runs this so a schedule that comes back is cleared
# again before it can fire. One-shot shutdowns survive "repeat cancel", so
# those are cancelled on their own.
set -u
PATH=/usr/bin:/bin:/usr/sbin:/sbin
PMSET="${INFOCUS_PMSET:-/usr/bin/pmset}"

sched=$("$PMSET" -g sched 2>/dev/null || true)
printf '%s\n' "$sched" | grep -q 'shutdown' || exit 0

"$PMSET" repeat cancel || true

printf '%s\n' "$sched" \
  | sed -n 's/.*shutdown at \([0-9][0-9]*\/[0-9][0-9]*\/[0-9][0-9]* [0-9:][0-9:]*\).*/\1/p' \
  | while IFS= read -r when; do
    [ -n "$when" ] || continue
    "$PMSET" schedule cancel shutdown "$when" || true
  done

"$PMSET" repeat wakeorpoweron MTWRF 07:00:00 || true
logger -t infocus-prevent-shutdown "cleared a scheduled shutdown" || true
