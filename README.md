<img width="64" height="64" alt="io github toccata" src="https://github.com/user-attachments/assets/e4860d8e-9fdd-476f-b1ec-7e183aed9ee1" /> 

# Toccata
Toccata is a GTK4 / libadwaita port of [cantata](https://github.com/nullobsi/cantata) by CDrummond and maintained by nullobsi

<img width="4096" height="2208" alt="screenshot" src="https://github.com/user-attachments/assets/88e827a3-2214-4d84-9299-638dd60cb0b9" />

## Installation
### 1. Dependencies (Arch)
    sudo pacman -S python-gobject gtk4 libadwaita python-mutagen
libadwaita 1.5 or newer is required.

### 2. Check, then run
    sh run.sh --check
This lists which dependencies are missing, where settings are read from, and
whether MPD is reachable. Then:
    sh run.sh
Set host, password, music folder and replay gain in **Preferences**

# Arch package
    sh packaging/arch/make-package.sh
    sudo pacman -U packaging/arch/toccata-*-any.pkg.tar.zst

## Status
Features ported from cantata include:
1. Full mpd client functionality
2. MPRIS integration with a tray icon (middle-click to play/pause, scroll to change volume)
3. Tag editing
4. Cover art browser
5. Info view with lyrics
6. Album grouped queue (optionally includes a compact mode as well)
7. Show (album)artists, albums playlists or search
8. Type-ahead search, as well as ctrl+(shift)+f search to filter the artists or the queue
9. Show composers in artists view for certain tags
10. Ignore certain prefixes when sorting (e.g. "The Beatles" will sort under B for "Beatles")
11. Filter artists by genre
12. Support for multiple partitions, collections and changing of MPD outputs

Some changes I would not port from cantata, unless there is interest:

- Dynamic/Smart playlists
- Podcasts, soundcloud etc.
- Internet radios
- MTP/USB support
- CD support
- Playback of non-MPD songs
- Ratings

## Screenshots

### Tag editor
<img width="4096" height="2208" alt="Tag editor" src="https://github.com/user-attachments/assets/600bbd53-1473-48fa-87ce-a79c37ed9300" />

### Info view
<img width="4096" height="2208" alt="Infor view" src="https://github.com/user-attachments/assets/7d9299ad-5843-4041-b9c3-aec1f51f3619" />

### Cover browser
<img width="2206" height="1368" alt="Cover browser" src="https://github.com/user-attachments/assets/58257c34-ba3d-4b79-b8b7-315a723ad7b5" />


