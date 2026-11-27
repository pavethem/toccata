#!/bin/sh
# Run a GTK script under a headless mutter (real Wayland + the GPU renderer, e.g.
# Vulkan) on its own socket and a private D-Bus: nothing appears on the user's
# screen. Usage: sh tests/gtk/run_headless_mutter.sh tests/gtk/gtk_smoke_x.py [args]
# TOCCATA_MONITOR=3840x2400 for a bigger (scaled) screen.
# (Broadway is enough for most tests; use this when the renderer matters.)
[ -z "$TOCCATA_PRIVATE_BUS" ] && exec env TOCCATA_PRIVATE_BUS=1 dbus-run-session -- sh "$0" "$@"
unset DISPLAY
export WAYLAND_DISPLAY=toccata-test-$$
mutter --headless --wayland --no-x11 --wayland-display="$WAYLAND_DISPLAY" --virtual-monitor "${TOCCATA_MONITOR:-1920x1200}" >/dev/null 2>&1 &
M=$!
for i in $(seq 50); do [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] && break; sleep 0.1; done
GDK_BACKEND=wayland timeout 120 python3 -X faulthandler "$@"
rc=$?
kill $M
exit $rc
