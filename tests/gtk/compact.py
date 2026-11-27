"""Run a GTK smoke test with the compact queue switched on (setting queue_compact):
    sh tests/gtk/run_headless_mutter.sh tests/gtk/compact.py tests/gtk/gtk_smoke_readd.py
The settings loader is wrapped before the app imports it, so the window starts
compact like it does for a user who chose "Compact Rows"."""
import os
import runpy
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT)
from toccata import settings  # noqa: E402

_load = settings.load


def _compact_load(*args, **kwargs):
    s = _load(*args, **kwargs)
    s.queue_compact = True
    return s


settings.load = _compact_load
script = sys.argv[1]
sys.argv = sys.argv[1:]
print("(compact queue)", flush=True)
runpy.run_path(script, run_name="__main__")
