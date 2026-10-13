#!/usr/bin/env python3
"""Patch HamClock V4.22: an ADS-B pane, no data credits on screen, the main clock in local time,
our version, no world map.

1. plot.cpp / dxpeds.cpp / contests.cpp: no data-source text on screen (weather attribution
   column, DXpeditions and Contests credit rows); the data and its readiness checks stay.
2. clocks.cpp: the main clock and its date show DE local time (all computing stays UTC);
   the UTC button reads LOC when the DE time zone is not UTC.
3. version.cpp: report V89.64 instead of upstream V4.22.
4. ADS-B, a pane choice like any other: a radar scope of the aircraft around DE that HamClock
   draws itself (adsb.cpp, from hamclock-adsb.cpp beside this script) from the feed
   tools/adsb_scope.py writes. PLOT_CH_ADSB and NV_ADSB_RANGE (its range) are appended to their
   enums, so saved pane choices and settings keep their numbers. Adafruit_RA8875::holdCanvas()
   lets a pane redraw as one frame for live viewers.
5. No world map: hcdisplay.py and the web page draw their own over its box, so HamClock makes,
   fetches and draws none. The box is erased once and left black; nothing counts as over the
   map, so no marker, spot, path, beacon or tap lands there; no map scale, no news banner, and
   the messages HamClock would show across it are only logged.

Idempotent: refuses if a hunk is not found exactly once. Run inside the ESPHamClock source dir.
"""
import os
import sys


def patch(path, old, new):
    s = open(path).read()
    if new in s:
        print("already patched", path)
        return
    n = s.count(old)
    if n != 1:
        sys.exit(f"{path}: expected 1 match, found {n}: {old[:70]!r}")
    open(path, "w").write(s.replace(old, new))
    print("patched", path)


def install(ours, path):
    """Our own source file `ours`, beside this script, as `path` in HamClock's source."""
    text = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ours)).read()
    if os.path.exists(path) and open(path).read() == text:
        print("already installed", path)
        return
    open(path, "w").write(text)
    print("installed", path)


patch("version.cpp",
      'const char *hc_version = "4.22";',
      'const char *hc_version = "89.64";')


patch("plot.cpp",
      "    const uint8_t attr_w = FONTW+1;     // allow for attribution down right side\n",
      "    const uint8_t attr_w = 0;           // influ: no attribution column, use the full width\n")

patch("plot.cpp",
      "    // attribution very small down the right side\n"
      "    selectFontStyle (LIGHT_FONT, FAST_FONT);\n"
      "    uint8_t ylen = strlen(wi.attribution);\n"
      "    uint16_t ly0 = box.y + (box.h - ylen*FONTH)/2;\n"
      "    for (uint8_t i = 0; i < ylen; i++) {\n"
      "        tft.setCursor (box.x+box.w-attr_w, ly0+i*FONTH);\n"
      "        tft.print (wi.attribution[i]);\n"
      "    }\n",
      "    // influ: no attribution down the right side\n")

patch("dxpeds.cpp",
      "    // credit\n"
      "    char credit[100];\n"
      "    int c_l = 0;\n"
      "    for (int i = 0; i < n_credits; i++)\n"
      "        c_l += snprintf (credit+c_l, sizeof(credit)-c_l, \"%s \", credits[i].name);\n"
      "    selectFontStyle (LIGHT_FONT, FAST_FONT);\n"
      "    tft.setTextColor(DXPEDS_COLOR);\n"
      "    uint16_t c_w = getTextWidth (credit);\n"
      "    tft.setCursor (box.x + (box.w-c_w)/2, box.y + CREDITS_Y0);\n"
      "    tft.print (credit);\n",
      "    // influ: no credit row\n")

patch("clocks.cpp",
      "    // break into components\n"
      "    tmElements_t tm_wo;\n"
      "    breakTime (t_wo, tm_wo);\n",
      "    // break into components -- influ: the main clock shows DE local time; t_wo stays UTC\n"
      "    tmElements_t tm_wo;\n"
      "    breakTime (t_wo + getTZ (de_tz), tm_wo);\n")

patch("clocks.cpp",
      "static void drawUTCButton()\n"
      "{\n"
      "    selectFontStyle (BOLD_FONT, FAST_FONT);\n",
      "static void drawUTCButton()\n"
      "{\n"
      "    // influ: main clock is DE local -- ask first, getTZ() may touch the drawing state\n"
      "    const char *zone = getTZ (de_tz) ? \"LOC\" : \"UTC\";\n"
      "\n"
      "    selectFontStyle (BOLD_FONT, FAST_FONT);\n")

patch("clocks.cpp",
      "        tft.setTextColor(RA8875_BLACK);\n"
      "        strcpy (msg, \"UTC\");\n",
      "        tft.setTextColor(RA8875_BLACK);\n"
      "        strcpy (msg, zone);\n")

patch("contests.cpp",
      "    // credit\n"
      "    selectFontStyle (LIGHT_FONT, FAST_FONT);\n"
      "    tft.setTextColor(CONTEST_COLOR);\n"
      "    uint16_t cw = getTextWidth(credit);\n"
      "    tft.setCursor (box.x + (box.w-cw)/2, box.y + CREDITS_Y0);\n"
      "    tft.print (credit);\n",
      "    // influ: no credit row\n")


# 4. the ADS-B pane

install("hamclock-adsb.cpp", "adsb.cpp")

patch("Makefile",
      "\tadif_parser.o \\\n",
      "\tadif_parser.o \\\n"
      "\tadsb.o \\\n")

patch("HamClock.h",
      '    X(PLOT_CH_DST,          "Disturbance")\n',
      '    X(PLOT_CH_DST,          "Disturbance")      \\\n'
      '    X(PLOT_CH_ADSB,         "ADS-B")            // influ: last, saved choices keep their numbers\n')

patch("HamClock.h",
      "extern bool checkMoonTouch (const SCoord &s, const SBox &box);\n",
      "extern bool checkMoonTouch (const SCoord &s, const SBox &box);\n"
      "\n"
      "// influ: adsb.cpp\n"
      "#define ADSB_INTERVAL   2                       // pane update interval, secs\n"
      "extern bool updateADSB (const SBox &box);\n"
      "extern bool checkADSBTouch (const SCoord &s, const SBox &box);\n")

patch("HamClock.h",
      "    NV_SAT2FLAGS,               // satellite 2 options\n"
      "\n"
      "    NV_N\n",
      "    NV_SAT2FLAGS,               // satellite 2 options\n"
      "    NV_ADSB_RANGE,              // influ: ADS-B pane range, miles\n"
      "\n"
      "    NV_N\n")

patch("nvram.cpp",
      "    1,                          // NV_SAT2FLAGS\n"
      "\n"
      "};\n",
      "    1,                          // NV_SAT2FLAGS\n"
      "    1,                          // NV_ADSB_RANGE (influ)\n"
      "\n"
      "};\n")

patch("plotmgmnt.cpp",
      "    case PLOT_CH_DST:           // fallthru\n"
      "        return (true);\n",
      "    case PLOT_CH_DST:           // fallthru\n"
      "    case PLOT_CH_ADSB:          // fallthru -- influ: the feed's absence is shown in the pane\n"
      "        return (true);\n")

patch("plotmgmnt.cpp",
      "        if (checkDXPedsTouch (s, box))\n"
      "            return (true);\n"
      "        in_top = true;\n"
      "        break;\n",
      "        if (checkDXPedsTouch (s, box))\n"
      "            return (true);\n"
      "        in_top = true;\n"
      "        break;\n"
      "    case PLOT_CH_ADSB:          // influ\n"
      "        if (checkADSBTouch (s, box))\n"
      "            return (true);\n"
      "        in_top = true;\n"
      "        break;\n")

patch("wifi.cpp",
      "                if (updateDST(box))\n"
      "                    next_update[pp] = nextPaneUpdate (pc, DST_INTERVAL);\n"
      "                else\n"
      "                    next_update[pp] = nextWiFiRetry(pc);\n"
      "            }\n"
      "            break;\n",
      "                if (updateDST(box))\n"
      "                    next_update[pp] = nextPaneUpdate (pc, DST_INTERVAL);\n"
      "                else\n"
      "                    next_update[pp] = nextWiFiRetry(pc);\n"
      "            }\n"
      "            break;\n"
      "\n"
      "        case PLOT_CH_ADSB:              // influ\n"
      "            if (t0 >= next_update[pp]) {\n"
      "                updateADSB (box);               // a local feed: can't fail\n"
      "                next_update[pp] = nextPaneUpdate (pc, ADSB_INTERVAL);\n"
      "            }\n"
      "            break;\n")

patch("ArduinoLib/Adafruit_RA8875.h",
      "        void drawPR(void);\n",
      "        void drawPR(void);\n"
      "\n"
      "        // influ: hold the canvas while a pane redraws, so the frame thread stages it whole\n"
      "        void holdCanvas (bool on) {\n"
      "            if (on)\n"
      "                pthread_mutex_lock (&fb_lock);\n"
      "            else {\n"
      "                fb_dirty = true;\n"
      "                pthread_mutex_unlock (&fb_lock);\n"
      "            }\n"
      "        }\n")


# 5. no world map: the map box is hcdisplay's, so HamClock neither makes nor fetches nor draws one

patch("earthmap.cpp",
      "void drawMoreEarth()\n"
      "{\n",
      "void drawMoreEarth()\n"
      "{\n"
      "    return;     // influ: no world map: no rows to sweep, no maps to fetch, rotate or redraw over\n")

patch("earthmap.cpp",
      "void initEarthMap()\n"
      "{\n",
      "void initEarthMap()\n"
      "{\n"
      "    // influ: no world map: the box stays black under hcdisplay's map; the DE and DX panes as ever\n"
      "    fillSBox (map_b, RA8875_BLACK);\n"
      "    updateClocks(true);\n"
      "    drawDEInfo();\n"
      "    drawDXInfo();\n"
      "    return;\n")

patch("ESPHamClock.cpp",
      "bool overMap (const SCoord &s)\n"
      "{\n",
      "bool overMap (const SCoord &s)\n"
      "{\n"
      "    return (false);     // influ: no world map, so nothing is over it: no marks, spots, paths or taps\n")

patch("ESPHamClock.cpp",
      "void drawAllSymbols()\n"
      "{\n"
      "    updateClocks(false);\n",
      "void drawAllSymbols()\n"
      "{\n"
      "    updateClocks(false);\n"
      "    return;             // influ: no world map to draw them on, nor its scale\n")

patch("spots.cpp",
      "void drawSpotLabelOnMap (DXSpot &spot, LabelOnMapEnd txrx, LabelOnMapDot dot)\n"
      "{\n",
      "void drawSpotLabelOnMap (DXSpot &spot, LabelOnMapEnd txrx, LabelOnMapDot dot)\n"
      "{\n"
      "    return;             // influ: no world map\n")

patch("spots.cpp",
      "void drawSpotPathOnMap (const DXSpot &spot)\n"
      "{\n",
      "void drawSpotPathOnMap (const DXSpot &spot)\n"
      "{\n"
      "    return;             // influ: no world map\n")

patch("rss.cpp",
      "void checkRSS(void)\n"
      "{\n",
      "void checkRSS(void)\n"
      "{\n"
      "    return;             // influ: no world map, so no news banner across its foot, nor its fetching\n")

patch("mapmanage.cpp",
      "    // log\n"
      "    Serial.printf (\"mapMsg: %s\\n\", msg);\n",
      "    // log\n"
      "    Serial.printf (\"mapMsg: %s\\n\", msg);\n"
      "    return;             // influ: no world map to show it over, nor to wait on\n")
