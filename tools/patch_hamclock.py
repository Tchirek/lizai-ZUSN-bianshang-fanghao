#!/usr/bin/env python3
"""Patch HamClock V4.22: no data credits on screen, the main clock in local time, our version.

1. plot.cpp / dxpeds.cpp / contests.cpp: no data-source text on screen (weather attribution
   column, DXpeditions and Contests credit rows); the data and its readiness checks stay.
2. clocks.cpp: the main clock and its date show DE local time (all computing stays UTC);
   the UTC button reads LOC when the DE time zone is not UTC.
3. version.cpp: report V89.64 instead of upstream V4.22.

Nothing is pushed into HamClock's panes any more (the ADS-B scope is laid over its pane by the
displays), so HamClock's own pane updates are left as they are upstream.

Idempotent: refuses if a hunk is not found exactly once. Run inside the ESPHamClock source dir.
"""
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
