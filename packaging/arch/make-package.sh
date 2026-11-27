#!/bin/sh
# Build the Arch package from the current source tree:
#   sh packaging/arch/make-package.sh      ->  packaging/arch/toccata-<ver>-<rel>-any.pkg.tar.zst
# Install it with:  sudo pacman -U packaging/arch/toccata-*.pkg.tar.zst
set -e
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
ver=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/toccata/__init__.py")
sed -i "s/^pkgver=.*/pkgver=$ver/" "$here/PKGBUILD"
tar -czf "$here/toccata-$ver.tar.gz" -C "$root" \
    --exclude=__pycache__ --exclude=packaging --exclude='*.pyc' \
    --transform "s,^\.,toccata-$ver," \
    ./toccata ./data ./tests ./README.md
sum=$(sha256sum "$here/toccata-$ver.tar.gz" | cut -d' ' -f1)
sed -i "s/^sha256sums=.*/sha256sums=('$sum')/" "$here/PKGBUILD"
cd "$here"
rm -f toccata-*.pkg.tar.zst
makepkg -f --cleanbuild --clean "$@"
