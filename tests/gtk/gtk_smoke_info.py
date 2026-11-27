"""GTK smoke test of the Info view (Ctrl+I): artist / album / lyrics columns for the
playing song, with canned web answers (no network). Run like gtk_smoke.py (headless:
sh tests/gtk/run_headless_mutter.sh tests/gtk/gtk_smoke_info.py); exits 1 on failure."""
import argparse, json, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
os.makedirs(os.path.join(_TMP, "cfg", "toccata"))
with open(os.path.join(_TMP, "cfg", "toccata", "settings.json"), "w") as f:
    json.dump({"lastfm_api_key": "TESTKEY", "tray_icon": False}, f)
os.chmod(os.path.join(_TMP, "cfg", "toccata", "settings.json"), 0o600)
sys.path.insert(0, PROJECT)
from tests import lazy_mock, search_mock
from tests.lazy_mock import LazyHandler, render

PLAYING = {"file": "rock/a1.flac"}  # what currentsong answers
QUEUE = [(1, "rock/a1.flac")]        # (id, uri) of the mock's queue
PLAYER_LOG = []                      # addid / playid commands received


class H(LazyHandler):  # serves the library (artists, albums) of search_mock's songs
    def respond(self, a):
        cmd = a[0]
        if cmd == "idle":
            return ""
        if cmd in ("noidle", "replay_gain_status", "outputs", "tagtypes"):
            return "OK\n"
        by = {s["file"]: s for s in self.db}
        if cmd == "status":
            return f"state: pause\nsongid: 1\nelapsed: 3.0\nduration: 100.0\nvolume: 50\nOK\n"
        if cmd == "currentsong":
            return render(dict(by[PLAYING["file"]], Id="1", Pos="0")) + "OK\n"
        if cmd == "playlistinfo":
            return "".join(render(dict(by[u], Id=str(i), Pos=str(p))) for p, (i, u) in enumerate(QUEUE)) + "OK\n"
        if cmd == "addid":
            new = 100 + len(QUEUE)
            QUEUE.append((new, a[1]))
            PLAYER_LOG.append(("addid", a[1]))
            return f"Id: {new}\nOK\n"
        if cmd == "playid":
            PLAYER_LOG.append(("playid", a[1]))
            return "OK\n"
        return super().respond(a)


def j(obj):
    return json.dumps(obj).encode()


ANSWERS = [
    # Wikipedia search (used while MusicBrainz is busy), for "Mass" only
    ("list=search&srsearch=Mass&", j({"query": {"search": [{"title": "Mass"}, {"title": "Mass (band)"}]}})),
    ("prop=extracts%7Cpageprops&ppprop=disambiguation&exintro=1&explaintext=1&redirects=1&format=json&formatversion=2&titles=Mass+%28band%29",
     j({"query": {"pages": [{"title": "Mass (band)", "extract": "Mass is a test rock band.\n== History ==\nx"}]}})),
    ("musicbrainz.org/ws/2/artist?", j({"artists": [{"id": "rocker-id", "name": "Rocker", "score": 100}]})),
    ("musicbrainz.org/ws/2/artist/rocker-id", j({"relations": [
        {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q1"}}]})),
    ("musicbrainz.org/ws/2/release-group?", j({"release-groups": [{"id": "first-id", "title": "First", "score": 100}]})),
    ("musicbrainz.org/ws/2/release-group/first-id", j({"relations": [
        {"type": "wikipedia", "url": {"resource": "https://en.wikipedia.org/wiki/First_(album)"}}]})),
    ("wikidata.org", j({"entities": {"Q1": {"sitelinks": {"dewiki": {"title": "Rocker (Band)"},
                                                          "enwiki": {"title": "Rocker (band)"}}}}})),
    ("en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1&explaintext=1&redirects=1&format=json&formatversion=2&titles=Rocker",
     j({"query": {"pages": [{"title": "Rocker (band)", "extract": "Rocker is a test band.\nFrom Testville.\n== History ==\nFounded 2000."}]}})),
    ("en.wikipedia.org", j({"query": {"pages": [{"title": "First (album)", "extract": "First is the first album."}]}})),
    ("audioscrobbler.com", j({"similarartists": {"artist": [{"name": "Jazzer"}, {"name": "Nobody Here"}]}})),
    ("lrclib.net/api/get", j({"plainLyrics": "eins\nzwei\ndrei", "syncedLyrics": "[00:01.00]eins\n[00:02.50]zwei\n[00:05.00]drei"})),
]


def main():
    H.db = search_mock.make_db()
    srv = lazy_mock.LazyServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import GLib, Gtk
    from toccata import app as appmod
    from toccata import infoview
    appmod.APP_ID = "io.github.toccata.smoke"
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=None, no_covers=True, no_highlight=False))
    failures, asked = [], []
    mb_busy = [False]

    def fake_get(url):
        asked.append(url)
        if mb_busy[0] and "musicbrainz.org" in url:
            import email.message
            import urllib.error
            raise urllib.error.HTTPError(url, 503, "Service Temporarily Unavailable", email.message.Message(), None)
        for part, data in ANSWERS:
            if part in url:
                return data
        return None

    def check(name, got, want):
        ok = got == want
        print("ok  " if ok else "FAIL", name, got, "" if ok else f"(wanted {want})", flush=True)
        if not ok:
            failures.append(name)

    def texts(box):  # what the user can see: mapped widgets only
        out, stack = [], [box]
        while stack:
            w = stack.pop(0)
            if not w.get_mapped():
                continue
            if isinstance(w, Gtk.Label):  # (a label may hold several paragraphs)
                out.extend(t for t in w.get_text().split("\n") if t) if w.get_text() else out.append("")
            elif isinstance(w, Gtk.LinkButton):
                out.append("<" + w.get_uri() + ">")
                continue
            elif isinstance(w, Gtk.Button) and w.get_label():
                out.append("[" + w.get_label() + "]")
                continue  # (not its label again)
            c = w.get_first_child()
            kids = []
            while c is not None:
                kids.append(c)
                c = c.get_next_sibling()
            stack = kids + stack
        return out

    def settled(iv):  # every part's lookup answered (its label no longer says "Looking ...")
        busy = [t for t in texts(iv.artist_wiki) + texts(iv.album_wiki) + [iv.lyrics_source.get_text()]
                if t.startswith("Looking")]
        return not busy and not any(t.is_alive() for t in threading.enumerate() if t.name.startswith("info-"))

    def steps():
        win = application.props.active_window
        win.set_size_request(1500, 1000)
        iv = win.info_view
        iv.get = fake_get
        iv.sleep = lambda s: None
        yield 2000
        win.refresh_status()
        yield 300
        check("nothing fetched while the Info view is closed", asked, [])
        win.activate_action("win.info", None)  # Ctrl+I
        for _ in range(40):
            yield 250
            if settled(iv):
                break
        check("Ctrl+I shows the Info view", (win.main_stack.get_visible_child_name(), win.info_btn.get_active()),
              ("info", True))
        left = texts(iv.artist_col.box)
        check("artist name", iv.artist_title.get_text(), "Rocker")
        check("artist article: English, the lead section only",
              [t for t in left if t in ("Rocker is a test band.", "From Testville.", "History", "Founded 2000.")],
              ["Rocker is a test band.", "From Testville."])
        check("link to the full article", "<https://en.wikipedia.org/wiki/Rocker_(band)>" in left, True)
        check("no German Wikipedia asked", any("de.wikipedia" in u for u in asked), False)
        sim = iv.similar_box
        check("similar artists: one line of text, no buttons",
              (sim.get_text(), [w for w in _walk(iv.artist_col.box) if isinstance(w, Gtk.Button)
                                and w.get_label() in ("Jazzer", "Nobody Here")]), ("Jazzer, Nobody Here", []))
        check("similar artists: in the library a link, else plain",
              ('href="toccata-artist:Jazzer"' in sim.get_label(), "Nobody Here</a>" in sim.get_label()), (True, False))
        check("albums in the library", "[First]" in left or "First" in left, True)
        mid = texts(iv.album_col.box)
        check("album title", iv.album_title.get_text(), "First")
        check("album article (direct Wikipedia link of the release group)", "First is the first album." in mid, True)
        check("album tracks", [r.title_label.get_text() for r in iv._track_rows], ["Alpha", "Beta"])
        check("playing track marked", [r.title_label.has_css_class("accent") for r in iv._track_rows], [True, False])
        check("lyrics source", iv.lyrics_source.get_text(), "From LRCLIB · synced")
        check("lyrics lines", iv.lyrics_label.get_text().split("\n"), ["eins", "zwei", "drei"])
        check("current line at 3.0 s is 'zwei'", iv.current_lyric(), "zwei")
        check("it is bold in the markup", '<span weight="bold"' in iv.lyrics_label.get_label()
              and iv.lyrics_label.get_label().split("<span")[1].endswith("zwei</span>\ndrei"), True)

        # the article and the lyrics can be selected as a whole (not one label per paragraph / line)
        def label_select_all(lbl):
            lbl.select_region(0, -1)
            got = lbl.get_selection_bounds()
            ok, a, b = got if len(got) == 3 else (bool(got), *got) if got else (False, 0, 0)
            return lbl.get_text()[a:b] if ok else None
        wiki_lbl = [w for w in _walk(iv.artist_wiki) if isinstance(w, Gtk.Label) and w.get_selectable()]
        check("the artist article is one selectable text (all paragraphs)",
              (len(wiki_lbl), label_select_all(wiki_lbl[0]) if wiki_lbl else None),
              (1, "Rocker is a test band.\n\nFrom Testville."))
        check("lyrics: one selectable text across all lines", label_select_all(iv.lyrics_label), "eins\nzwei\ndrei")
        iv.lyrics_label.select_region(2, 9)  # "ns\nzwei"
        iv._current_line = -2  # the highlight moves (redrawn from the markup) ...
        iv._follow()
        got = iv.lyrics_label.get_selection_bounds()
        check("... and the user's selection stays", (got[0], iv.lyrics_label.get_text()[got[1]:got[2]]) if len(got) == 3 else got,
              (True, "ns\nzwei"))
        # every text block is laid out at its full height (a TextView once got 0 px)
        blocks = [w for col in (iv.artist_col, iv.album_col, iv.lyrics_col) for w in _walk(col.box)
                  if isinstance(w, Gtk.Label) and w.get_selectable()]
        short = [(type(w).__name__, w.get_height(), w.measure(Gtk.Orientation.VERTICAL, w.get_width())[1])
                 for w in blocks if w.get_height() < w.measure(Gtk.Orientation.VERTICAL, w.get_width())[1]]
        check("no text block is cut short", (len(blocks) >= 3, short), (True, []))
        check("no text block is taller than its text",
              [(w.get_height(), w.measure(Gtk.Orientation.VERTICAL, w.get_width())[1]) for w in blocks
               if w.get_height() > w.measure(Gtk.Orientation.VERTICAL, w.get_width())[1] + 2], [])
        check("the highlight survives selecting", iv.current_lyric(), "zwei")
        # clicking a track plays it; one that is not in the queue is added first (as in Cantata)
        rows = {r.title_label.get_text(): r for r in iv._track_rows}
        rows["Beta"].emit("clicked")
        yield 300
        check("click on a track not in the queue: added, then played",
              PLAYER_LOG, [("addid", "rock/a2.flac"), ("playid", "101")])
        PLAYER_LOG.clear()
        rows["Alpha"].emit("clicked")
        yield 300
        check("click on a track in the queue: just played", PLAYER_LOG, [("playid", "1")])
        check("Last.fm asked with the key", any("audioscrobbler" in u and "TESTKEY" in u for u in asked), True)
        n = len(asked)
        win.refresh_status()  # same song again: nothing new is fetched
        yield 500
        check("same song: no new requests", len(asked), n)

        # another song of another artist
        PLAYING["file"] = "jazz/b1.flac"
        win.refresh_status()
        for _ in range(40):
            yield 250
            if settled(iv):
                break
        check("song change: the artist follows", iv.artist_title.get_text(), "Jazzer")
        check("song change: the album follows", iv.album_title.get_text(), "Night")

        # a similar artist that is in the library opens it in the Artists view
        PLAYING["file"] = "rock/a1.flac"
        win.refresh_status()
        for _ in range(40):
            yield 250
            if settled(iv):
                break
        iv.similar_box.emit("activate-link", "toccata-artist:Jazzer")  # what clicking the link does
        yield 1200
        check("similar artist click: Info off, artist shown",
              (win.main_stack.get_visible_child_name(), win.art_sel.get_selected_item().get_string()), ("main", "Jazzer"))
        win.activate_action("win.info", None)
        yield 600
        check("Ctrl+I again shows it", win.main_stack.get_visible_child_name(), "info")

        # MusicBrainz busy (503 on every try): a message and a Retry button; Retry works
        mb_busy[0] = True
        n = len(asked)
        PLAYING["file"] = "special/alphabet soup.flac"  # Chef: not looked up before
        win.refresh_status()
        for _ in range(60):
            yield 250
            if settled(iv):
                break
        tried = [u for u in asked[n:] if "/ws/2/artist?" in u]
        check("busy: the artist search was tried 1 + 3 times", len(tried), 4)
        shown = texts(iv.artist_wiki)
        check("busy: says so, with a Retry button",
              shown, ["MusicBrainz (used to find the Wikipedia article) is busy right now.", "[Retry]"])
        check("busy: similar artists from Last.fm are shown anyway",
              ("Jazzer" in iv.similar_box.get_text(), iv.similar_note.get_text()),
              (True, "From Last.fm. Artists in your library are links."))
        mb_busy[0] = False
        retry = next(b for b in _walk(iv.artist_wiki) if isinstance(b, Gtk.Button) and b.get_label() == "Retry")
        n = len(asked)
        retry.emit("clicked")
        for _ in range(60):
            yield 250
            if settled(iv):
                break
        check("Retry: MusicBrainz asked again, and answered (no exact match for 'Chef' in the canned search)",
              (any("/ws/2/artist?" in u for u in asked[n:]), texts(iv.artist_wiki)),
              (True, ["No Wikipedia article found for this artist."]))

        # MusicBrainz busy, but Wikipedia's search finds the artist (Cantata's way)
        mb_busy[0] = True
        n = len(asked)
        PLAYING["file"] = "many/000.flac"  # Mass: not looked up before
        win.refresh_status()
        for _ in range(60):
            yield 250
            if settled(iv):
                break
        check("busy: the article found by the Wikipedia search instead",
              texts(iv.artist_wiki), ["Mass is a test rock band.", "<https://en.wikipedia.org/wiki/Mass_(band)>"])
        check("busy: MusicBrainz was tried first", any("/ws/2/artist?" in u for u in asked[n:]), True)
        check("busy: the album, not found by the search either, says busy",
              texts(iv.album_wiki), ["MusicBrainz (used to find the Wikipedia article) is busy right now.", "[Retry]"])
        mb_busy[0] = False
        win.activate_action("win.info", None)
        yield 300
        check("and Ctrl+I hides it", win.main_stack.get_visible_child_name(), "main")
        print("FAILED" if failures else "ALL OK", flush=True)
        application.quit()

    def _walk(w):
        c = w.get_first_child()
        while c is not None:
            yield c
            yield from _walk(c)
            c = c.get_next_sibling()

    gen = None

    def tick():
        try:
            GLib.timeout_add(next(gen), tick)
        except StopIteration:
            pass
        except Exception:
            import traceback
            traceback.print_exc(); failures.append("exception"); application.quit()
        return False

    def act(_a):
        nonlocal gen
        if gen is None:
            gen = steps(); GLib.timeout_add(10, tick)
    application.connect("activate", act)
    application.run([sys.argv[0]])
    sys.stdout.flush()
    os._exit(1 if failures else 0)


main()
