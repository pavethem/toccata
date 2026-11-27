#!/bin/sh
# Make GNOME show toccata with its icon (dock, Activities, window list):
# installs the .desktop file and the icons into ~/.local/share. Run from source,
# no root needed.  sh install-desktop.sh            (install / update)
#                  sh install-desktop.sh --remove   (undo)
set -e
here=$(cd "$(dirname "$0")" && pwd)
data=${XDG_DATA_HOME:-$HOME/.local/share}
app=io.github.toccata
icons="$data/icons/hicolor"
if [ "$1" = "--remove" ]; then
    rm -f "$data/applications/$app.desktop" "$icons/scalable/apps/$app.svg" \
          "$icons/symbolic/apps/$app-symbolic.svg"
    echo "removed"
else
    mkdir -p "$data/applications" "$icons/scalable/apps" "$icons/symbolic/apps"
    sed "s|@RUN_SH@|$here/run.sh|" "$here/data/$app.desktop.in" > "$data/applications/$app.desktop"
    cp "$here/data/icons/hicolor/scalable/apps/$app.svg" "$icons/scalable/apps/"
    cp "$here/data/icons/hicolor/symbolic/apps/$app-symbolic.svg" "$icons/symbolic/apps/"
    echo "installed $data/applications/$app.desktop"
fi
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t -f "$icons" 2>/dev/null || true
command -v update-desktop-database >/dev/null && update-desktop-database -q "$data/applications" 2>/dev/null || true
