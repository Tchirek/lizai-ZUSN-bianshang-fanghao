#!/bin/sh
# build_deb.sh VERSION HAMCLOCK_BINARY [OUT_DIR]
#
# Pack this checkout as a Debian package, influ_VERSION_amd64.deb, for Debian 13 (trixie):
#   /usr/share/influ/      the code, as in the repository (hamclock, tools, sdrmux, scheduler,
#                          collector, analysis) and deploy/station.example.json
#   /usr/lib/influ/hamclock-web   HamClock V4.22 built with tools/patch_hamclock.py (MIT; its
#                          licence in the copyright file); pass the binary you built
#   /usr/lib/systemd/user/ the units from deploy/, running the code above with the system python3
#   /usr/sbin/influ-admin  the root-level helper, as deploy/influ-admin
# Everything a station keeps of its own stays in ~/influ (station.json, model/, data/, private/),
# as before; the package never writes there. Needs dpkg-deb and git.
set -eu

VERSION=$1
HAMCLOCK=$2
OUT=${3:-.}
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
PKG=$WORK/influ_${VERSION}_amd64

mkdir -p "$PKG/DEBIAN" "$PKG/usr/share/influ" "$PKG/usr/lib/influ" "$PKG/usr/lib/systemd/user" \
         "$PKG/usr/sbin" "$PKG/usr/share/doc/influ" "$PKG/usr/share/applications"

# the code, as committed (no caches, nothing private)
git -C "$ROOT" -c core.autocrlf=false archive HEAD hamclock tools sdrmux scheduler collector analysis \
    deploy/station.example.json | tar -x -C "$PKG/usr/share/influ"
install -m 755 "$HAMCLOCK" "$PKG/usr/lib/influ/hamclock-web"
strip --strip-unneeded --remove-section=.comment --remove-section=.note "$PKG/usr/lib/influ/hamclock-web"

# the units: the code from /usr/share/influ, HamClock from /usr/lib/influ, python3 from the
# system; the station's own files (env files, data) still under %h/influ
for unit in "$ROOT"/deploy/*.service "$ROOT"/deploy/*.timer; do
    sed -e 's#%h/influ/\.venv/bin/python#/usr/bin/python3#g' \
        -e 's#%h/influ/hamclock/bin/hamclock-web#/usr/lib/influ/hamclock-web#g' \
        -e '/^\(ExecStart\|WorkingDirectory\)=/s#%h/influ/\(hamclock\|tools\|sdrmux\|scheduler\|collector\|analysis\)#/usr/share/influ/\1#g' \
        "$unit" > "$PKG/usr/lib/systemd/user/$(basename "$unit")"
done
install -m 755 "$ROOT/deploy/influ-admin" "$PKG/usr/sbin/influ-admin"
install -m 644 "$ROOT/deploy/influ-hamclock.desktop" "$PKG/usr/share/applications/influ-hamclock.desktop"
gzip -9n -c "$ROOT/README.md" > "$PKG/usr/share/doc/influ/README.md.gz"
install -m 644 "$ROOT/LICENSE" "$PKG/usr/share/doc/influ/LICENSE"
gzip -9n > "$PKG/usr/share/doc/influ/changelog.gz" <<EOF
influ ($VERSION) unstable; urgency=low

  * Release $VERSION, from commit $(git -C "$ROOT" rev-parse --short HEAD).

 -- Tchirek <tchirek@users.noreply.github.com>  $(date -R)
EOF

cat > "$PKG/usr/share/doc/influ/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: influ
Source: https://github.com/Tchirek/lizai-ZUSN-bianshang-fanghao

Files: *
License: AGPL-3.0-only
 The full text is in /usr/share/doc/influ/LICENSE.

Files: usr/lib/influ/hamclock-web usr/share/influ/hamclock/vendor/hamclock-font/*
Copyright: 2020-2025 Elwood Charles Downey
License: MIT
$(sed 's/^/ /; s/^ $/ ./' "$ROOT/hamclock/vendor/hamclock-font/LICENSE")

Files: usr/share/influ/hamclock/vendor/openlayers/*
License: BSD-2-Clause
$(sed 's/^/ /; s/^ $/ ./' "$ROOT/hamclock/vendor/openlayers/LICENSE.md")

Files: usr/share/influ/hamclock/vendor/amiri/*
Copyright: 2010-2022 The Amiri Project Authors
License: OFL-1.1
$(sed 's/^/ /; s/^ $/ ./' "$ROOT/hamclock/vendor/amiri/OFL.txt")
EOF

INSTALLED=$(du -sk "$PKG/usr" | cut -f1)
cat > "$PKG/DEBIAN/control" <<EOF
Package: influ
Version: $VERSION
Architecture: amd64
Maintainer: Tchirek <tchirek@users.noreply.github.com>
Installed-Size: $INSTALLED
Depends: python3 (>= 3.11), python3-numpy, python3-pandas, python3-scipy, python3-pil, python3-pygame, python3-websocket, python3-serial, python3-aprslib, python3-pypinyin, libc6, libstdc++6
Recommends: xfonts-base, python3-pywayland, rtl-sdr, wsjtx, dump1090-fa | readsb
Suggests: piaware
Section: hamradio
Priority: optional
Homepage: https://github.com/Tchirek/lizai-ZUSN-bianshang-fanghao
Description: amateur station on one RTL-SDR, time-shared with ADS-B feeding
 One RTL-SDR stays on 1090 MHz feeding ADS-B; a scheduler takes it to the
 2 m band for seconds at a time when no aircraft is due, to hear FT8 and
 watch the band, and a UV-K5 sends FT8. A patched HamClock runs the station
 clock, with an ADS-B scope pane and, in place of its world map, a zoomable
 terrain map of the radio's line-of-sight coverage and live amateur activity,
 on the machine's own screen and in a web page.
EOF
cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v systemctl >/dev/null; then
    systemctl --global daemon-reload >/dev/null 2>&1 || true
fi
EOF
chmod 755 "$PKG/DEBIAN/postinst"

find "$PKG" -type d -exec chmod 755 {} +
find "$PKG/usr/share" "$PKG/usr/lib/systemd" -type f -exec chmod 644 {} +
find "$PKG/usr/share/influ" -name '*.sh' -exec chmod 755 {} +
dpkg-deb --root-owner-group --build "$PKG" "$OUT" >/dev/null
echo "$OUT/influ_${VERSION}_amd64.deb"
