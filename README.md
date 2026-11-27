# Toccata

Toccata is a GTK4 / libadwaita MPD client for GNOME, with tag editing.

## 1. Unpack
    unzip toccata.zip
    cd toccata

You should see: `run.sh`, `README.md`, `toccata/` (the program) and
`tests/`. Everything below is run from inside this `toccata` folder.

## 2. Install dependencies (Arch)
    sudo pacman -S python-gobject gtk4 libadwaita python-mutagen
libadwaita 1.5 or newer is required.

## 3. Check, then run
    sh run.sh --check
This lists which dependencies are missing, where settings are read from, and
whether MPD is reachable. Then:
    sh run.sh
Set host, password, music folder and replay gain in **Preferences** (menu button
top right, or Ctrl+,). No command-line flags are needed after that.
`--music-dir` must be your MPD music directory. It is needed for tag editing
when connecting over TCP. 
## Arch package
    sh packaging/arch/make-package.sh
    sudo pacman -U packaging/arch/toccata-*-any.pkg.tar.zst
Builds from this source tree (runs the tests too). Installs the `toccata`
command, the menu entry and the icons; the program lives in `/usr/share/toccata`.
The version comes from `toccata/__init__.py`. If you used `install-desktop.sh`
before, run `sh install-desktop.sh --remove` so the menu starts the package.
Remove with `sudo pacman -R toccata`.

## 4. Tests (optional, no GTK needed)
    python3 -m unittest discover -s tests -t .

## Status
| Part | State |
|---|---|
| MPD protocol client | tested against a mock server |
| Editing rules (capitalise, various-artists, renumber, ...) | tested; deliberate deviations listed in `toccata/tagops.py` |
| Tag read/write via mutagen | written, not yet run |
| Cover display (player bar): folder art, then embedded picture; background fetch, LRU cache | fetch/cache logic tested; widget code written, not yet run |
| Settings file, flag/env override rules | tested |
| Idle watcher (stoppable thread), reconnect-and-retry | tested against mock server, repeated runs |
| Preferences dialog (widgets) | written, not yet run |
| Library model: group by album artist (fallback: artist), natural sort order, albums by year, track order | tested (`library.py`) |
| Streaming `listallinfo` loader, command lists (add album / replace queue) | tested against mock server |
| Lazy loading (names up front, one artist at a time, windowed `find`), output-buffer limit simulated | tested against a mock MPD with a real database |
| Last artist remembered and restored | tested (settings + window logic) |
| Divider positions remembered and restored (only the user's drags, not GTK's own moves) | tested (`layout.py`, settings, window logic) |
| Window size and maximized state remembered and restored | tested (`layout.py`, settings, window logic) |
| MPD outputs, partitions (per connection, incl. idle) and collections (saved server profiles) | logic tested against a stateful mock MPD; dialogs exercised against a fake GTK, never rendered |
| Queue: the playing / paused song is highlighted (accent title, play or pause icon, tinted row) | marker logic tested (`playback.py`); row widgets written, never run or rendered |
| Library pane: artists -> albums with covers, tracks on expand | written; logic exercised against a fake GTK, never rendered |
| GTK UI (split view: library left, queue right; player bar; tag dialog) | runs (reported working); newest parts not yet run |

If the cover ever misbehaves, `sh run.sh --no-covers` turns the feature off completely.

Covers need MPD >= 0.21 (folder art, e.g. cover.jpg) or >= 0.22 (pictures embedded in tags); `--check` tells you what your server supports.

Not done: covers in the queue list, smart/dynamic playlists, MPRIS, streams, lyrics,
scrobbling, ratings, CD ripping, device sync.

## Info view

The **i** button in the header bar (or Ctrl+I) replaces library and queue with three
columns about the song in use (the playing one; when stopped, the queue's current one):

- **Artist**: the lead section of the album artist's English Wikipedia article (a link
  leads to the whole article), similar artists from Last.fm
  (Toccata's built-in key or your own, Preferences → Cover Search; those in your library are
  links to them in the Artists view), and your albums by the artist.
- **Album**: the cover, the lead of the album's Wikipedia article and its tracks (the playing one
  marked). Clicking a track plays it; if it is not in the queue it is added at the end first.
- **Lyrics**: from the file's own tags, a `.lrc` / `.txt` file next to it (needs the music
  folder), else from [LRCLIB](https://lrclib.net). Synced lyrics highlight the current
  line and follow the song (not for 5 s after you scroll them yourself).

Articles are found through MusicBrainz and Wikidata (MusicBrainz ids in your tags are
used directly), so a band called "Low" gets its own article. When MusicBrainz is busy
or does not know the artist or album, a Wikipedia search is used instead (as Cantata
does), and a page is only taken if it is no disambiguation page and its first paragraph
fits (a band, singer, ...; an album by this artist). Always the English
Wikipedia (the interface is English). Only artist, album and track names are sent. Results are kept in `~/.cache/toccata/info`
(30 days; "nothing found" for a week). Lookups only run while the view is shown.

## Albums
The **Albums** tab shows every album as a cover grid. Sort it by **Recently added**
(the default; newest first, loaded bit by bit as you scroll, so MPD is never asked for
the whole library), Artist, Title or Year. Double-click adds an album to the queue;
the buttons below add, play, add to a playlist, show it in the Artists tab or edit
its tags. Albums can be dragged onto the queue, and typing jumps to an album.

## Search
The **Search** tab searches MPD's database. Pick what to
search in: Artist, Composer, Performer, Album, Title, Genre, Comment, Date, Original
Date, Modified, File or Any. Composer, Performer, Comment and Original Date appear only
if your MPD knows those tags. **Modified** takes a number of days ("7" = the last week)
or a date (2015/01/31). **Any** also matches file names and paths. Press Enter to
search. Each result shows its album cover. At most 5000 results are shown. Double-click or "+" adds to the queue; the
buttons act on the selected results, or on all of them; the ⋮ menu has Add to
Playlist, Locate in Library and Edit Tags. Results can be dragged onto the queue.

## Removing from the queue
Select tracks in the queue and press Delete (or click a row's − button). The next
track is selected afterwards, so you can keep pressing Delete.

## Finding an artist
Ctrl+F (or the search button next to the genre list) opens a filter above the artist
list: type words, and only artists whose name contains all of them stay ("3 of 1200");
case and accents do not matter. The first match is shown if the current artist is
filtered out; Enter moves on to the list. Escape closes the filter and shows every
artist again. In the Search tab, Ctrl+F goes to the search field instead.

## Finding songs in the queue
Ctrl+Shift+F (or the search button above the queue) opens a filter: type words,
and only songs whose title, album, artist or composer contain all of them stay
visible ("12 of 340"). `#1999` or `#1990-1999` filters by year. Escape closes it.

## Queue grouped by album
In the queue's ⋮ menu, **Group Albums** groups the queue by album:
consecutive tracks of one album get a header with cover, "Album Artist – Album
(Year)" and the track count and duration. ▾ collapses an album to one row; **Collapse
All / Expand All Albums** are in the same menu. Click a header to select the album
(Ctrl+click adds it to the selection), drag it to move the whole album. A collapsed
album counts as all its tracks for Remove, Edit tags and drag and drop.

## Playlists
The **Playlists** tab lists MPD's stored playlists. Select one to see its tracks;
consecutive tracks of one album are grouped under a header (⋮ → Group Albums to
switch it off; Collapse/Expand All Albums).
Add to queue / Play act on the selected tracks or the whole playlist; drag a playlist
or tracks onto the queue to insert them there. The ⋮ menu has Rename, Remove
Duplicates, Remove Invalid Tracks (files no longer in the database) and Delete.
Create playlists with **Save as playlist…** under the queue, or with the "+" button
under the library (selected albums, else the whole artist) and in search.

## Type-ahead
Works in the artist and album lists and in the Playlists view (playlist names and
their tracks): the list that has the focus is searched.
Click into the library and start typing an artist's name: the list jumps to the first
match (case, accents and punctuation do not matter; "beatles" also finds "The Beatles").
Type the same letter again to step through artists starting with it. Backspace
corrects, Escape cancels, a short pause starts a new search.

## Composers
Preferences → Interface → "Use the composer for these genres" (e.g. "Classical, Opera"):
songs of these genres that have a composer tag are listed under their composer in the
Artists view instead of under the album artist (orchestra, ensemble, soloist). Empty
= off.

## Sorting without "The"
Preferences → Interface → "Ignore prefixes when sorting" (default: The). Artists and
albums starting with one of these words sort as if it came last: "The Beatles" sorts
under B. Several prefixes are separated by commas ("The, Die, Les").

## Genre filter
The drop-down above the artists ("All Genres") shows only one genre:
only artists that have songs of that genre, and in their albums only
those songs. Ctrl+G opens it; type to find a genre. The choice is remembered.

## Media controls, tray icon
toccata speaks MPRIS, so GNOME's media controls (top bar / lock screen), media keys
and tools like KDE Connect can control it. With a tray (on GNOME: the "AppIndicator and
KStatusNotifierItem Support" extension) it shows a tray icon: click to show or hide the
window, middle-click to play/pause, scroll for volume, right-click for a menu. Closing
the window then keeps it running in the tray; quit from the tray menu or with Ctrl+Q.
Both can be switched off in Preferences → Interface.

To get the app icon in the dock and in Activities, run once:
    sh install-desktop.sh            (sh install-desktop.sh --remove to undo)

## Right-click menus and setting a cover
Right-click an artist, an album or a track (Artists view) or an album tile (Albums
view) for Add to Queue, Play, Add to Playlist, Edit Tags and more. On an album,
**Set Cover…** searches Cover Art Archive (MusicBrainz), Deezer and iTunes for
"artist album" (you can change the text) and shows the covers found; **Choose File…**
in that window takes an image file from your computer instead. The chosen cover is
saved as `cover.jpg` (or `.png`) in the album's folder, where MPD finds it; an existing
cover is kept as `cover.jpg.old`. Only the search text is sent to these services.
Last.fm is searched too, with Toccata's built-in API key. It is shared by all users;
when Last.fm reports its rate limit, Toccata pauses Last.fm for a few minutes. You can
enter your own free key in Preferences → Cover Search instead
(create one at https://www.last.fm/api/account/create).
This needs the music folder set in Preferences (and write access to it).

## Editing tags of several tracks
With several tracks selected, the tag editor has a track list at the top: **All tracks**
(a value typed there is set for every track; fields marked "multiple values" keep each
track's own value unless you type one) and each track on its own. Switch with the list,
the arrow buttons or Alt+Left / Alt+Right; edits are kept while switching, tracks with
unsaved edits show a "•", and nothing is written before Save.

The **ReplayGain** button at the bottom of the tag editor shows the tracks'
ReplayGain values (gain and peak, track and album). They are read-only: a scanner such
as rsgain writes them, and MPD uses them when Replay gain is on in Preferences.

## Queue menu
Right-click a track in the queue (or an album header when the queue is grouped) for
Play, **Edit Tags…** (all selected tracks), **Show in Artists**, **Show in Albums**,
Add to Playlist… and Remove. Right-clicking a selected track acts on the whole
selection. Show in Albums switches "Recently added" to sorting by artist when that
album has not been loaded yet.

## Play options
The four buttons right of the volume switch MPD's play options: **Repeat** (start the
queue again after the last song), **Single** (stop after the current song; with Repeat:
repeat that song), **Shuffle** and **Consume** (remove songs from the queue once
played). They always show MPD's current state, also when another client changes it.

## Cover preview
Rest the mouse on the cover in the player bar to see the whole album art large.

## Library
Loading is lazy, because asking MPD for every song at once (`listallinfo`)
overruns MPD's output buffer (`max_output_buffer_size`, 8 MiB by default) on
big libraries:
- At start only the **artist names** are fetched (small).
- An artist's songs are fetched when that artist is shown, in windows of 1000
  songs. Visited artists are cached; the cache is dropped when MPD's database
  changes.
- On start the **artist you last selected** is shown again (saved as
  `last_artist` in the settings file). Album covers are loaded only for the
  albums on screen.

Rules:
- An artist is the song's **album artist**; if that tag is empty, its artist.
  The list sorts by name, ignoring case, punctuation and accents, with numbers
  sorted as numbers. (The AlbumArtistSort tag is only used inside an
  artist's own data, not for the artist list order.)
- An artist's **albums** are shown with cover art, sorted by year then name;
  albums without a year come last. Open an album to see its tracks.
- Albums are told apart by MusicBrainz album id, else by name.

Songs without an album artist tag need MPD >= 0.21 (filter expressions) to be
found under their artist. `sh run.sh --check` shows how many such songs you
have and whether your server supports this.

## Server menu: outputs, partitions, collections
The menu button (top right) has **Outputs…**, **Partitions…** and **Collections…**.
They open one dialog, "MPD server", on the matching page.
- **Outputs**: a switch per output of the current partition. Outputs that belong
  to other partitions are listed too, with **Move here**. Switching is done by
  output *name*, because MPD renumbers ids when outputs move between partitions.
- **Partitions** (MPD 0.22 or newer): see, switch, create and delete partitions.
  A partition belongs to a connection, so this window switches only itself; the
  queue and playback state you see are those of that partition. `default` and
  the partition in use cannot be deleted. If a saved partition no longer exists
  you are told and the window uses `default`.
- **Collections**: saved MPD servers. A collection remembers host,
  port, password, music folder, **partition** and **last artist**; switching
  reconnects and brings those back. Preferences edits the active collection.
  Command-line flags and `MPD_HOST` etc. still override the active one, and
  the dialog says so. The window subtitle shows the collection (when there is
  more than one) and the partition.
Not done: "basic" collections that start their own MPD, and editing
output attributes. `sh run.sh --check` lists your partitions and outputs.

## Remembered layout
On the next start you get back: the last artist, the divider between library
and queue, the divider between artists and albums, the window size, and
whether the window was maximized. All of it is kept in the settings file and
written about a second after a change and when you close the window.
- Dividers: GTK's own moves of a divider (first layout, resizing the window) are
  deliberately not saved, so your choice survives window resizing.
- Window: the normal size and the maximized state are kept separately, so
  un-maximizing gives back your old window size, not a screen-sized one.
  Fullscreen changes nothing. Positions and sizes are in pixels.
- The window's position on screen is not remembered (on Wayland, apps cannot
  place their own windows anyway).

## Settings
Saved in `~/.config/toccata/settings.json` (or `$XDG_CONFIG_HOME/toccata/`).
**The password is stored unencrypted**; the file is readable only by you (0600).

Precedence: command-line flag > environment (`MPD_HOST`, `MPD_PORT`,
`TOCCATA_MUSIC_DIR`; `MPD_HOST` may be `password@host`) > saved settings.
Preferences shows a note when a value is currently overridden.
MPD has no user names, only a password.

Replay gain: None / Track / Album / Auto, read from the
server when the dialog opens, plus "apply on every connect" because MPD resets
it when it restarts.

Not done in the settings: HTTP stream URL, crossfade, volume step.

## Licence
GPL-2.0-or-later. `toccata/tagops.py` contains ported code; its header names the
original and its copyright holder.

## Troubleshooting a GTK crash

If it aborts with a `Gtk:ERROR` message, run `python3 -X faulthandler -m toccata`
(the Python stack is then printed), and bisect with `sh run.sh --no-highlight` and
`sh run.sh --no-covers`. A bad `window_*` / `*_split` value in
`~/.config/toccata/settings.json` can also be ruled out by moving that file away.

## Drag and drop

Drag an artist, or the selected albums, onto the queue to insert them where you drop (needs MPD 0.23 for the position; older servers refuse). Drag queue entries (the whole selection) to reorder. Logic tested in `dnd.py`; the GTK drag/drop glue has never been run.
