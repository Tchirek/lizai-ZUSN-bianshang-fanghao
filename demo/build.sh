#!/bin/sh
# Put the showcase page together in dist/: public/ as it is, the summits and the station's own
# OpenLayers beside it. Cloudflare Pages runs this (root directory demo, output directory dist).
set -eu
cd "$(dirname "$0")"
rm -rf dist
mkdir -p dist/vendor/openlayers
cp -R public/. dist/
cp sites.json dist/data/sites.json
cp ../hamclock/vendor/openlayers/ol.js ../hamclock/vendor/openlayers/ol.css ../hamclock/vendor/openlayers/LICENSE.md \
   dist/vendor/openlayers/
