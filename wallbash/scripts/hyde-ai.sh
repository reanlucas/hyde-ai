#!/usr/bin/env bash
# @name: hyde-ai-theme
# @short: Wallbash post-render hook for the hyde-ai sidebar
#
# Invoked by ~/.config/hyde/wallbash/always/hyde-ai.dcol after wallbash
# renders ~/.cache/hyde/wallbash/hyde-ai.css.
#
# The sidebar watches that CSS file with a Gio.FileMonitor and restyles
# itself, so this hook is intentionally minimal. It exists to:
#   1. guarantee the render target directory exists (color.set.sh
#      silently skips any template whose target parent dir is missing),
#   2. nudge an ALREADY-RUNNING sidebar over D-Bus as a belt-and-braces
#      fallback for a missed or garbage-collected file-monitor event.
#
# It must never fail loudly and never start the sidebar: wallbash runs
# hooks backgrounded and disowned (`bash -c "$cmd" & disown`), so the
# exit code is discarded and stderr may be lost anyway.

# Wallbash invokes this with no arguments, but `hyde-shell wallbash
# hyde-ai` can pass some through. hyde-shell inspects "$1" while being
# sourced (its `case "$1" in ... init) ... exit 0` runs before the
# is-sourced guard), so clear the positional parameters first.
set --

if ! source "$(command -v hyde-shell)" 2>/dev/null; then
    # Not fatal: during a real render color.set.sh has already exported
    # pkg_installed/print_log and the dcol_* palette for us.
    :
fi

# print_log is exported by color.set.sh; provide a no-op if we were run
# in some context where it is not defined.
if ! declare -F print_log >/dev/null 2>&1; then
    print_log() { :; }
fi

cacheDir="${cacheDir:-$HOME/.cache/hyde}"
cssFile="${cacheDir}/wallbash/hyde-ai.css"

# 1. Keep the render target's parent directory alive. color.set.sh:133
#    returns early (silently) if this is missing, which would make the
#    template look like it simply stopped working.
mkdir -p "${cacheDir}/wallbash" 2>/dev/null

# dcol_* are exported during a real render but NOT when this script is
# run manually via `hyde-shell wallbash hyde-ai`. Fall back to the live
# palette so manual invocations behave identically.
if [[ -z ${dcol_pry1:-} ]]; then
    # shellcheck source=/dev/null
    [ -r "${cacheDir}/wall.dcol" ] && source "${cacheDir}/wall.dcol"
fi

[ -r "$cssFile" ] || {
    print_log -sec "wallbash" -warn "hyde-ai" "stylesheet not rendered yet: $cssFile"
    exit 0
}

# 2. Best-effort live nudge.
#
# The application id the sidebar registers on the session bus. Override
# with HYDE_AI_APP_ID if the app uses a different one.
appId="${HYDE_AI_APP_ID:-dev.hyde.HydeAi}"
objPath="/${appId//.//}"

command -v gdbus >/dev/null 2>&1 || exit 0

# NameHasOwner asks whether the app is ALREADY on the bus. Unlike
# `gapplication action`, it will not D-Bus-activate the sidebar, so a
# theme change can never pop the panel open on its own.
owned="$(gdbus call --session \
    --dest org.freedesktop.DBus \
    --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.NameHasOwner "$appId" 2>/dev/null)"

case "$owned" in
*true*) ;;
*) exit 0 ;;
esac

# Activate the app's "reload-theme" GAction. A no-op if the app does not
# export one; the sidebar's own file monitor remains the primary path.
gdbus call --session \
    --dest "$appId" \
    --object-path "$objPath" \
    --method org.gtk.Actions.Activate \
    "reload-theme" "[]" "{}" >/dev/null 2>&1

print_log -sec "wallbash" -stat "hyde-ai" "restyled for ${dcol_mode:-unknown} mode"

exit 0
