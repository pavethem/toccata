"""Environment self-check: python3 -m toccata --check
Prints what is available and what is missing, without opening a window."""
import sys


def _line(ok, label, detail=""):
    print(f"[{'OK' if ok else 'FEHLT'}] {label}" + (f": {detail}" if detail else ""))
    return ok


def run():
    all_ok = True
    _line(sys.version_info >= (3, 9), "Python", sys.version.split()[0])

    try:
        import gi
        _line(True, "PyGObject", gi.__version__)
    except ImportError as e:
        _line(False, "PyGObject", f"{e} (Arch: sudo pacman -S python-gobject)")
        gi = None
        all_ok = False

    if gi:
        try:
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
            _line(True, "GTK 4", f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")
        except (ValueError, ImportError) as e:
            _line(False, "GTK 4", f"{e} (Arch: sudo pacman -S gtk4)")
            all_ok = False
        try:
            gi.require_version("Adw", "1")
            from gi.repository import Adw
            major, minor = Adw.get_major_version(), Adw.get_minor_version()
            ok = (major, minor) >= (1, 5)
            _line(ok, "libadwaita (>= 1.5 noetig)", f"{major}.{minor}")
            all_ok &= ok
        except (ValueError, ImportError) as e:
            _line(False, "libadwaita", f"{e} (Arch: sudo pacman -S libadwaita)")
            all_ok = False

    try:
        import mutagen
        _line(True, "mutagen", mutagen.version_string)
    except ImportError:
        _line(False, "mutagen", "Tag-Bearbeitung nicht moeglich (Arch: sudo pacman -S python-mutagen)")
        all_ok = False

    from .mpd_client import MPDClient, MPDError, describe_error
    from .settings import config_path, effective, load
    eff, overrides = effective(load())
    host, port, password = eff.connection()
    src = ", ".join(f"{k} aus {v}" for k, v in overrides.items()) or "nur gespeicherte Einstellungen"
    _line(True, "Einstellungsdatei", f"{config_path()} ({src})")
    c = MPDClient(host, port, password, timeout=3)
    try:
        c.connect()
        _line(True, f"MPD erreichbar unter {host}:{port}", f"Protokoll {c.protocol_version}")
        try:
            ver = tuple(int(x) for x in c.protocol_version.split(".")[:2])
        except ValueError:
            ver = (0, 0)
        _line(ver >= (0, 21), "Cover aus Ordnern (albumart, MPD >= 0.21)",
              "unterstuetzt" if ver >= (0, 21) else "Server ist zu alt: keine Cover")
        _line(ver >= (0, 22), "Eingebettete Cover (readpicture, MPD >= 0.22)",
              "unterstuetzt" if ver >= (0, 22) else "nicht unterstuetzt (nur Ordner-Cover)")
        from .library import NO_ALBUM_ARTIST, count_songs, fetch_artist_names
        _line(True, "Kuenstler (Album-Artist, sonst Artist)", str(len(fetch_artist_names(c))))
        missing = count_songs(c, NO_ALBUM_ARTIST)
        if missing is None:
            _line(True, "Titel ohne Album-Artist-Tag",
                  "nicht pruefbar (MPD < 0.21): solche Titel koennen in der Bibliothek fehlen")
        else:
            _line(True, "Titel ohne Album-Artist-Tag", f"{missing} (erscheinen unter ihrem Artist)")
        parts = c.listpartitions()
        _line(True, "Partitionen (MPD >= 0.22)",
              "nicht unterstuetzt" if parts is None else f"{len(parts)}: {', '.join(parts)}")
        try:
            outs = c.all_outputs()
            _line(True, "Ausgaenge", ", ".join(f"{o.name}{'' if o.enabled else ' (aus)'}" for o in outs) or "keine")
        except (OSError, MPDError) as e:
            _line(False, "Ausgaenge", describe_error(e, host, port))
        md = c.music_directory()
        _line(bool(md), "MPD meldet Musikverzeichnis",
              md or "nicht verfuegbar (bei TCP normal) -> --music-dir angeben")
    except (OSError, MPDError) as e:
        _line(False, f"MPD unter {host}:{port}", describe_error(e, host, port))
        all_ok = False
    finally:
        c.close()

    print("\nAlles bereit." if all_ok else "\nEs fehlt noch etwas (siehe oben).")
    return 0 if all_ok else 1
