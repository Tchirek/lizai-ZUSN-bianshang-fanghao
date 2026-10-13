Search uses the same Courier Prime Sans bitmap glyphs as HamClock's DX cluster
(the 1600×960 build, scaled as the clock is at 1280×768), with the 1988 ISAS
16-dot Song face for Chinese. The outlines preserve the bitmap pixels.

Sources were copied from the station's HamClock build and Debian xfonts-base.
See HamClock-LICENSE and ISAS-LICENSE. Rebuild search.ttf with
`python tools/build_search_font.py`; fontTools and Pillow are build dependencies
only. The font is served locally and shared by the native and web search.
