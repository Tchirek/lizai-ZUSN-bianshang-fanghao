#!/usr/bin/env python3
"""Patch HamClock V4.22 so a picture posted with set_bmp owns its pane cleanly.

1. HamClock.h / wifi.cpp: web_bmp_pane[] flag; updateWiFi() skips updates and rotation of a
   flagged pane; setPlotChoice() (an explicit new choice) clears the flag.
2. webserver.cpp: set_bmp sets the flag on success, set_bmp&off clears it.
3. plot.cpp: installBMPBox() no longer pre-fills the box black (the pixel buffer always covers
   the whole box), which made every redraw flash black, border included, for live viewers.
4. clocks.cpp: the top-of-the-hour band-conditions redraw skips a flagged pane.
5. plot.cpp / dxpeds.cpp / contests.cpp: no data-source text on screen (weather attribution
   column, DXpeditions and Contests credit rows); the data and its readiness checks stay.

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


patch("HamClock.h",
      "extern PlotChoice plot_ch[PANE_N];         // current choice in each pane, or PLOT_CH_NONE for PANE_0\n",
      "extern PlotChoice plot_ch[PANE_N];         // current choice in each pane, or PLOT_CH_NONE for PANE_0\n"
      "extern bool web_bmp_pane[PANE_N];          // influ: pane shows a set_bmp picture, hold its updates\n")

patch("wifi.cpp",
      "time_t next_update[PANE_N];                     // next function call\n",
      "time_t next_update[PANE_N];                     // next function call\n"
      "bool web_bmp_pane[PANE_N];                      // influ: pane owned by a set_bmp picture\n")

patch("wifi.cpp",
      "        // handy\n"
      "        const SBox &box = plot_b[pp];\n"
      "        PlotChoice pc = plot_ch[pp];\n"
      "\n"
      "        // rotate if this pane is rotating and it's time",
      "        // handy\n"
      "        const SBox &box = plot_b[pp];\n"
      "        PlotChoice pc = plot_ch[pp];\n"
      "\n"
      "        // influ: a picture posted with set_bmp owns this pane until set_bmp&off or a new\n"
      "        // choice, so neither scheduled updates nor rotation may paint over it\n"
      "        if (web_bmp_pane[pp])\n"
      "            continue;\n"
      "\n"
      "        // rotate if this pane is rotating and it's time")

patch("wifi.cpp",
      "    // ignore if new choice is already in some other pane\n"
      "    PlotPane pp_now = findPaneForChoice (pc);\n"
      "    if (pp_now != PANE_NONE && pp_now != pp)\n"
      "        return (false);\n",
      "    // ignore if new choice is already in some other pane\n"
      "    PlotPane pp_now = findPaneForChoice (pc);\n"
      "    if (pp_now != PANE_NONE && pp_now != pp)\n"
      "        return (false);\n"
      "\n"
      "    // influ: an explicit new choice releases a set_bmp picture\n"
      "    web_bmp_pane[pp] = false;\n")

patch("webserver.cpp",
      "            // \"off\" means restore pane content\n"
      "            scheduleNewPlot(plot_ch[pane]);\n",
      "            // \"off\" means restore pane content\n"
      "            web_bmp_pane[pane] = false;\n"
      "            scheduleNewPlot(plot_ch[pane]);\n")

patch("webserver.cpp",
      "            if (!installBMPBox (gr, plot_b[pane], fit, ynot)) {\n"
      "                quietStrncpy (line, ynot.get(), line_len);\n"
      "                return (false);\n"
      "            }\n"
      "        }",
      "            if (!installBMPBox (gr, plot_b[pane], fit, ynot)) {\n"
      "                quietStrncpy (line, ynot.get(), line_len);\n"
      "                return (false);\n"
      "            }\n"
      "            web_bmp_pane[pane] = true;          // influ: hold the pane's own updates\n"
      "        }")

patch("plot.cpp",
      "    // prep box with black\n"
      "    fillSBox (box, RA8875_BLACK);\n",
      "    // influ: no black prefill -- readBMPImage always returns pixels for the whole box, and\n"
      "    // filling first made every redraw flash black (border included) for live viewers\n")

patch("clocks.cpp",
      "            if (bc_pane != PANE_NONE && bc_matrix.ok)\n"
      "                plotBandConditions (plot_b[bc_pane], 0, NULL, NULL);",
      "            if (bc_pane != PANE_NONE && bc_matrix.ok && !web_bmp_pane[bc_pane])  // influ\n"
      "                plotBandConditions (plot_b[bc_pane], 0, NULL, NULL);")

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

patch("contests.cpp",
      "    // credit\n"
      "    selectFontStyle (LIGHT_FONT, FAST_FONT);\n"
      "    tft.setTextColor(CONTEST_COLOR);\n"
      "    uint16_t cw = getTextWidth(credit);\n"
      "    tft.setCursor (box.x + (box.w-cw)/2, box.y + CREDITS_Y0);\n"
      "    tft.print (credit);\n",
      "    // influ: no credit row\n")
