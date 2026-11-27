"""GTK smoke test: the tag editor with several tracks - "All tracks" and each track
on its own (as Cantata), edits kept while switching, written on Save. Uses tiny FLAC
files made with ffmpeg in a temporary folder. Run like gtk_smoke.py (broadway +
dbus-run-session)."""
import os, subprocess, sys, tempfile
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
sys.path.insert(0, PROJECT)
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk
from toccata import app as appmod, tags

paths = []
RG = {1: ["-metadata", "REPLAYGAIN_TRACK_GAIN=-7.23 dB", "-metadata", "REPLAYGAIN_TRACK_PEAK=0.988",
          "-metadata", "REPLAYGAIN_ALBUM_GAIN=-6.50 dB", "-metadata", "REPLAYGAIN_ALBUM_PEAK=1.0"], 2: [], 3: []}
for i, (title, artist) in enumerate((("One", "A"), ("Two", "B"), ("Three", "B")), 1):
    p = os.path.join(_TMP, f"{i:02d}.flac")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "0.1"] + RG[i] + [
                    "-metadata", f"title={title}", "-metadata", f"artist={artist}", "-metadata", "album=Hits",
                    "-metadata", f"tracknumber={i}", p], check=True)
    paths.append(p)


class FakeWindow(Adw.ApplicationWindow):
    written = None

    def after_tag_write(self, songs, changed, errors):
        FakeWindow.written = (changed, errors)


def main():
    app = Adw.Application(application_id="io.github.toccata.smoke")
    report = []

    def activate(_a):
        win = FakeWindow(application=app)
        win.present()
        merged = tags.merge_for_edit([tags.read_tags(p) for p in paths])
        ed = appmod.TagEditor(win, [None] * 3, paths, merged)
        ed.present(win)
        rows = ed.rows
        pages = lambda: [ed.pages.get_string(i) for i in range(ed.pages.get_n_items())]
        report.append(("pages", pages(), "artist title", rows["artist"].get_title(), "hint", ed.page_hint.get_text()))
        def rg_texts():
            ed._fill_replaygain()
            out, stack = [], [ed.rg_popover.get_child()]
            while stack:
                w = stack.pop(0)
                if isinstance(w, Gtk.Label):
                    out.append(w.get_text())
                c = w.get_first_child()
                while c is not None:
                    stack.append(c)
                    c = c.get_next_sibling()
            return out
        report.append(("rg all tracks", rg_texts()))
        rows["album"].set_text("Greatest Hits")         # all tracks
        ed.step(1)                                       # -> track 1
        report.append(("track 1", rows["title"].get_text(), rows["album"].get_text(), "hint", ed.page_hint.get_text(),
                       "prev ok", ed.prev_btn.get_sensitive()))
        report.append(("rg track 1", rg_texts()))
        rows["title"].set_text("Uno")
        ed.step(1); ed.step(1)                           # -> track 3
        report.append(("track 3", rows["title"].get_text(), "next ok", ed.next_btn.get_sensitive(), "pages", pages()))
        report.append(("rg track 3", rg_texts()))
        rows["artist"].set_text("C")
        ed.picker.set_selected(0)                        # back to all tracks
        report.append(("all again: album", rows["album"].get_text(), "artist", rows["artist"].get_text(),
                       rows["artist"].get_title()))
        ed.on_save(None)
        report.append(("saved", FakeWindow.written,
                       [(t["title"], t["artist"], t["album"]) for t in map(tags.read_tags, paths)]))
        report.append(("replaygain kept after save", tags.read_replaygain(paths[0])))
        for line in report:
            print(*line, sep=" | ")
        app.quit()
    app.connect("activate", activate)
    app.run([sys.argv[0]])


main()
