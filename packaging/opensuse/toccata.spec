#
# spec file for package toccata (openSUSE Leap 16.x / Tumbleweed)
# Built from the local source tree: run ./make-package.sh (it makes the
# tarball, fills in the version and calls rpmbuild).
#
Name:           toccata
Version:        1.0.2
Release:        1
Summary:        GTK4/libadwaita MPD client with Cantata-style tag editing
License:        GPL-2.0-or-later
Group:          Productivity/Multimedia/Sound/Players
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch
# the program lives in /usr/share/toccata, not in site-packages, so the
# generic python dependency generators and bytecompile hooks must stay out
%global __brp_python_bytecompile %{nil}
%global __pythondist_requires %{nil}
%global __python_requires %{nil}
%global __python_provides %{nil}
%if 0%{?suse_version}
BuildRequires:  python3
BuildRequires:  python3-mutagen
%endif
Requires:       python3 >= 3.11
Requires:       python3-gobject
Requires:       python3-gobject-Gdk
Requires:       python3-mutagen
Requires:       libadwaita-1-0 >= 1.5
Requires:       typelib-1_0-Adw-1
Requires:       typelib-1_0-Gtk-4_0
Requires:       typelib-1_0-GdkPixbuf-2_0
Requires:       typelib-1_0-Graphene-1_0
Requires:       typelib-1_0-Pango-1_0
Requires:       hicolor-icon-theme
Suggests:       mpd

%description
Toccata is an MPD client for GNOME written in Python with GTK 4 and
libadwaita. It includes the tag editing of Cantata (track, album,
artist, genre, renaming of files).

%prep
%setup -q

%build
sed 's|^Exec=.*|Exec=toccata %%U|' data/io.github.toccata.desktop.in > io.github.toccata.desktop

%install
install -d %{buildroot}%{_datadir}/%{name}
cp -r toccata data %{buildroot}%{_datadir}/%{name}/
find %{buildroot}%{_datadir}/%{name} -name __pycache__ -prune -exec rm -rf {} +
%if 0%{?suse_version}
# pyc files only when built on openSUSE: they must match its python3
python3 -m compileall -q -o 0 -o 1 -s %{buildroot} -p / %{buildroot}%{_datadir}/%{name}/toccata
%endif

# launcher; -P: a "toccata" folder in the current directory cannot shadow it
install -d %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/%{name} <<END
#!/bin/sh
export PYTHONPATH=%{_datadir}/%{name}\${PYTHONPATH:+:\$PYTHONPATH}
exec /usr/bin/python3 -P -m toccata "\$@"
END
chmod 755 %{buildroot}%{_bindir}/%{name}

install -Dm644 io.github.toccata.desktop -t %{buildroot}%{_datadir}/applications
install -Dm644 data/icons/hicolor/scalable/apps/io.github.toccata.svg \
    -t %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
install -Dm644 data/icons/hicolor/symbolic/apps/io.github.toccata-symbolic.svg \
    -t %{buildroot}%{_datadir}/icons/hicolor/symbolic/apps

%check
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t . -q

%files
%doc README.md
%{_bindir}/%{name}
%{_datadir}/%{name}
%{_datadir}/applications/io.github.toccata.desktop
%{_datadir}/icons/hicolor/scalable/apps/io.github.toccata.svg
%{_datadir}/icons/hicolor/symbolic/apps/io.github.toccata-symbolic.svg

%changelog
* Fri Nov 27 2026 Tino
- 1.0.2: the artist list no longer jumps to the bottom after a database
  update while the window was covered by other windows

* Fri Nov 27 2026 Tino
- 1.0.1: album hover buttons no longer appear on an empty-space click;
  the artist list scrolls to the new artist after an update made in another view

* Sun Nov 15 2026 Tino
- local build from the source tree
