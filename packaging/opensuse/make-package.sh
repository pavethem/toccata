#!/bin/sh
# Build the openSUSE (Leap 16.x / Tumbleweed) RPM from the current source tree:
#   sh packaging/opensuse/make-package.sh   ->  packaging/opensuse/toccata-<ver>-<rel>.noarch.rpm
# Works on openSUSE (zypper in rpm-build) and on Arch (pacman -S rpm-tools).
# Install on openSUSE with:  sudo zypper in ./toccata-*.noarch.rpm
set -e
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
ver=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/toccata/__init__.py")
sed -i "s/^Version:.*/Version:        $ver/" "$here/toccata.spec"
top=$(mktemp -d)
trap 'rm -rf "$top"' EXIT
mkdir -p "$top/SOURCES"
tar -czf "$top/SOURCES/toccata-$ver.tar.gz" -C "$root" \
    --exclude=__pycache__ --exclude=packaging --exclude='*.pyc' \
    --transform "s,^\.,toccata-$ver," \
    ./toccata ./data ./tests ./README.md
# outside openSUSE the Requires (typelib-1_0-..., python3-gobject) do not
# exist, so do not check build dependencies there
nodeps=
[ -f /etc/SUSE-brand ] || grep -qi suse /etc/os-release 2>/dev/null || nodeps=--nodeps
rpmbuild -bb $nodeps --define "_topdir $top" "$@" "$here/toccata.spec"
rm -f "$here"/toccata-*.noarch.rpm
cp "$top"/RPMS/noarch/toccata-*.noarch.rpm "$here/"
ls -l "$here"/toccata-*.noarch.rpm
