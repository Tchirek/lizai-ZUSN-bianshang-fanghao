/* influ: the ADS-B pane, PLOT_CH_ADSB: a radar scope of the aircraft around DE.
 *
 * tools/adsb_scope.py follows dump1090 and writes what it hears to our_dir/influ-adsb.txt every
 * second, and the base map (water, rivers, rail, roads, runways, cities, towns, airfields) once to
 * our_dir/influ-adsb-map.txt. This pane reads them and draws the scope itself: an azimuthal
 * equidistant view about DE filling the box, small text in its corners, as the Moon and SDO panes
 * are laid out. Tapping below the top edge chooses the range, kept in NV_ADSB_RANGE.
 *
 * The scope is drawn with the canvas held (tft.holdCanvas), so live viewers get each new scope
 * whole: it is redrawn every ADSB_INTERVAL seconds, too often to show it being drawn.
 *
 * Feed, a line each (# comments):
 *   T unix_time aircraft_heard_today sdr_away(0|1)
 *   N aircraft_heard_now_without_a_position
 *   A hex lat lon alt_ft(-1 unknown, -2 on the ground) track_deg(-1 unknown) speed_kt category mlat(0|1) name
 *   P lat lon lat lon ...                  the trail of the A line above, oldest first
 * Map:
 *   L kind lat lon lat lon ...             kind: water river rail trunk motorway runway
 *   C lat lon name                         city
 *   W lat lon                              town
 *   R lat lon name                         airfield
 */

#include <algorithm>
#include <string>
#include <vector>
#include <sys/stat.h>

#include "HamClock.h"

#define FEED_FN         "influ-adsb.txt"
#define MAP_FN          "influ-adsb-map.txt"
#define FEED_STALE      30                      // secs before the feed counts as stopped
#define DEF_RANGE_MI    30                      // range until one is chosen
#define MI_KM           1.609344F
#define EARTH_KM        6371.0F

#define TITLE_COLOR     RGB565(255,125,0)       // as DE_COLOR
#define RING_COLOR      RGB565(41,40,98)        // range rings: the map grid's blue
#define EDGE_COLOR      GRAY                    // outer ring and ticks: as the pane border
#define TEXT_COLOR      RGB565(110,110,110)     // corner and legend text
#define CITY_COLOR      RGB565(70,150,70)
#define TOWN_COLOR      RGB565(45,90,45)
#define AWAY_COLOR      RA8875_RED

static const uint8_t ranges_mi[] = {10, 20, 30, 50, 100};

// the base map, dim so the aircraft stay the brightest thing on the scope; drawn in this order
typedef struct {
    const char *kind;
    uint16_t color;
    uint8_t thick;                              // raw px
} MapStyle;
static const MapStyle map_styles[] = {
    {"water",    RGB565(20,45,110),   1},
    {"river",    RGB565(30,70,160),   1},
    {"rail",     RGB565(70,70,70),    1},
    {"trunk",    RGB565(75,55,20),    1},
    {"motorway", RGB565(120,85,25),   1},
    {"runway",   RGB565(175,175,175), 3},
};

// altitude bands, top of each in feet, and their colours; unknown is white
typedef struct {
    int top;
    uint16_t color;
    const char *label;
} AltBand;
static const AltBand alt_bands[] = {
    {2000,  RGB565(255,64,64),  "0"},
    {10000, RGB565(246,129,49), "2"},
    {20000, RGB565(255,255,0),  "10"},
    {30000, RGB565(0,255,0),    "20"},
    {99999, RGB565(0,255,255),  "30k"},
};

typedef struct {
    float lat, lng;                             // degrees
} Spot;

typedef struct {
    int x, y, w, h;                             // app px, may reach past the box
} Area;

typedef struct {
    uint8_t style;                              // map_styles[] index
    std::vector<Spot> spots;
} MapLine;

typedef struct {
    uint8_t kind;                               // 'C', 'W' or 'R'
    Spot spot;
    std::string name;
} MapPlace;

typedef struct {
    char hex[8], cat[4], name[12];
    Spot spot;
    int alt;                                    // ft, -1 unknown, -2 on the ground
    int track;                                  // degrees, -1 unknown
    int speed;                                  // kt
    bool mlat;                                  // position from MLAT or TIS-B
    std::vector<Spot> trail;                    // oldest first
} Aircraft;

typedef struct {
    time_t t;
    int today, nopos;
    bool away;
    std::vector<Aircraft> aircraft;
} Feed;

// the scope's geometry in raw px
typedef struct {
    int16_t cx, cy, r;
    float km;                                   // range
} Scope;

static uint8_t range_mi;                        // 0 until read from NV
static std::vector<MapLine> map_lines;
static std::vector<MapPlace> map_places;
static time_t map_mtime;


/* our_dir/fn
 */
static std::string ourFile (const char *fn)
{
    std::string path = our_dir;
    if (path.empty() || path.back() != '/')
        path += '/';
    return (path + fn);
}

/* the range in miles, from NV once
 */
static uint8_t rangeMi (void)
{
    if (range_mi == 0) {
        uint8_t mi = 0;
        bool known = false;
        if (NVReadUInt8 (NV_ADSB_RANGE, &mi))
            for (unsigned i = 0; i < NARRAY(ranges_mi); i++)
                known = known || ranges_mi[i] == mi;
        range_mi = known ? mi : DEF_RANGE_MI;
    }
    return (range_mi);
}

/* read "lat lon lat lon ..." from s into spots
 */
static void readSpots (const char *s, std::vector<Spot> &spots)
{
    Spot sp;
    int n;
    while (sscanf (s, "%f %f%n", &sp.lat, &sp.lng, &n) == 2) {
        spots.push_back (sp);
        s += n;
    }
}

/* the text after the first n blank-separated fields of s, else ""
 */
static const char *afterFields (const char *s, int n)
{
    while (n-- > 0) {
        while (*s == ' ')
            s++;
        while (*s && *s != ' ')
            s++;
    }
    while (*s == ' ')
        s++;
    return (s);
}

/* read the base map if its file is new or changed
 */
static void readMap (void)
{
    std::string path = ourFile (MAP_FN);
    struct stat st;
    if (stat (path.c_str(), &st) < 0 || st.st_mtime == map_mtime)
        return;
    FILE *fp = fopen (path.c_str(), "r");
    if (!fp)
        return;
    map_lines.clear();
    map_places.clear();
    map_mtime = st.st_mtime;

    char *line = NULL;
    size_t len = 0;
    while (getline (&line, &len, fp) > 0) {
        line[strcspn (line, "\r\n")] = '\0';
        char kind[16];
        MapPlace pl;
        if (line[0] == 'L' && sscanf (line, "L %15s", kind) == 1) {
            for (unsigned i = 0; i < NARRAY(map_styles); i++) {
                if (strcmp (kind, map_styles[i].kind) == 0) {
                    MapLine ml;
                    ml.style = i;
                    readSpots (afterFields (line, 2), ml.spots);
                    if (ml.spots.size() > 1)
                        map_lines.push_back (ml);
                    break;
                }
            }
        } else if (line[0] && strchr ("CWR", line[0]) && sscanf (line+1, "%f %f", &pl.spot.lat, &pl.spot.lng) == 2) {
            pl.kind = line[0];
            pl.name = afterFields (line, 3);
            map_places.push_back (pl);
        }
    }
    free (line);
    fclose (fp);
    Serial.printf ("ADSB: map %d lines %d places\n", (int)map_lines.size(), (int)map_places.size());
}

/* read the feed; false if there is none
 */
static bool readFeed (Feed &feed)
{
    FILE *fp = fopen (ourFile(FEED_FN).c_str(), "r");
    if (!fp)
        return (false);
    feed.t = 0;
    feed.today = feed.nopos = 0;
    feed.away = false;
    feed.aircraft.clear();

    char *line = NULL;
    size_t len = 0;
    while (getline (&line, &len, fp) > 0) {
        line[strcspn (line, "\r\n")] = '\0';
        long t;
        int away;
        Aircraft ac;
        int mlat;
        switch (line[0]) {
        case 'T':
            if (sscanf (line, "T %ld %d %d", &t, &feed.today, &away) == 3) {
                feed.t = t;
                feed.away = away != 0;
            }
            break;
        case 'N':
            sscanf (line, "N %d", &feed.nopos);
            break;
        case 'A':
            if (sscanf (line, "A %7s %f %f %d %d %d %3s %d", ac.hex, &ac.spot.lat, &ac.spot.lng, &ac.alt,
                                        &ac.track, &ac.speed, ac.cat, &mlat) == 8) {
                ac.mlat = mlat != 0;
                quietStrncpy (ac.name, afterFields (line, 9), sizeof(ac.name));
                feed.aircraft.push_back (ac);
            }
            break;
        case 'P':
            if (!feed.aircraft.empty())
                readSpots (line+1, feed.aircraft.back().trail);
            break;
        default:
            break;
        }
    }
    free (line);
    fclose (fp);
    return (feed.t > 0);
}

/* where spot is on the scope, raw px; whether it is within it.
 * azimuthal equidistant about DE: distance and initial bearing kept.
 */
static bool project (const Scope &sc, const Spot &sp, int16_t &x, int16_t &y)
{
    float lat2 = deg2rad (sp.lat);
    float dlng = deg2rad (sp.lng) - de_ll.lng;
    float a = sinf((lat2-de_ll.lat)/2)*sinf((lat2-de_ll.lat)/2) + cosf(de_ll.lat)*cosf(lat2)*sinf(dlng/2)*sinf(dlng/2);
    float km = 2*EARTH_KM*asinf(sqrtf(fminf(1, a)));
    float b = atan2f (sinf(dlng)*cosf(lat2), cosf(de_ll.lat)*sinf(lat2) - sinf(de_ll.lat)*cosf(lat2)*cosf(dlng));
    float r = km/sc.km*sc.r;
    x = roundf (sc.cx + r*sinf(b));
    y = roundf (sc.cy - r*cosf(b));
    return (km <= sc.km);
}

/* colour of an altitude, and that colour dimmed for trails and vectors
 */
static uint16_t altColor (int alt)
{
    if (alt == -1)
        return (RA8875_WHITE);
    if (alt == -2)
        return (alt_bands[0].color);
    for (unsigned i = 0; i < NARRAY(alt_bands); i++)
        if (alt < alt_bands[i].top)
            return (alt_bands[i].color);
    return (alt_bands[NARRAY(alt_bands)-1].color);
}

static uint16_t dimColor (uint16_t c)
{
    return (RGB565 (RGB565_R(c)*45/100, RGB565_G(c)*45/100, RGB565_B(c)*45/100));
}

/* text in the FAST font at app x, y (its top left) in color
 */
static void fastText (int16_t x, int16_t y, uint16_t color, const char *str)
{
    tft.setTextColor (color);
    tft.setCursor (x, y);
    tft.print (str);
}

/* whether a is clear of every area in taken
 */
static bool clear (const Area &a, const std::vector<Area> &taken)
{
    for (const Area &t : taken)
        if (!(a.x+a.w < t.x || a.x > t.x+t.w || a.y+a.h < t.y || a.y > t.y+t.h))
            return (false);
    return (true);
}

/* the base map within the scope: lines whose both ends are in it, then cities and airfields named
 * where they are clear of what is taken (the aircraft's labels), which they add to
 */
static void drawMap (const Scope &sc, const SBox &box, std::vector<Area> &taken)
{
    for (unsigned s = 0; s < NARRAY(map_styles); s++) {
        const MapStyle &ms = map_styles[s];
        for (const MapLine &ml : map_lines) {
            if (ml.style != s)
                continue;
            int16_t x0, y0, x1, y1;
            bool in0 = project (sc, ml.spots[0], x0, y0);
            for (size_t i = 1; i < ml.spots.size(); i++) {
                bool in1 = project (sc, ml.spots[i], x1, y1);
                if (in0 && in1)
                    tft.drawLineRaw (x0, y0, x1, y1, ms.thick, ms.color);
                x0 = x1, y0 = y1, in0 = in1;
            }
        }
    }

    const int k = tft.SCALESZ;
    for (int pass = 0; pass < 2; pass++) {              // cities first, so towns never crowd them out
        for (const MapPlace &pl : map_places) {
            if ((pass == 0) != (pl.kind == 'C') || pl.kind == 'R')
                continue;
            int16_t x, y;
            if (!project (sc, pl.spot, x, y))
                continue;
            if (pl.kind == 'W') {
                tft.drawPixelRaw (x, y, TOWN_COLOR);
                continue;
            }
            tft.fillRectRaw (x-1, y-1, 3, 3, CITY_COLOR);
            if (pl.name.empty())
                continue;
            Area b = {x/k + 2, y/k - 4, getTextWidth (pl.name.c_str()), 9};
            if (b.x + b.w < box.x + box.w - 2 && clear (b, taken)) {
                taken.push_back (b);
                fastText (b.x, b.y, CITY_COLOR, pl.name.c_str());
            }
        }
    }
    std::vector<std::string> done;
    for (const MapPlace &pl : map_places) {
        int16_t x, y;
        if (pl.kind != 'R' || !project (sc, pl.spot, x, y))
            continue;
        bool seen = false;
        for (const std::string &d : done)
            seen = seen || d == pl.name;
        Area b = {x/k + 3, y/k + 1, getTextWidth (pl.name.c_str()), 9};
        if (!seen && b.x + b.w < box.x + box.w - 2 && clear (b, taken)) {
            done.push_back (pl.name);
            taken.push_back (b);
            fastText (b.x, b.y, map_styles[NARRAY(map_styles)-1].color, pl.name.c_str());
        }
    }
}

/* range rings, ticks every 30 degrees, compass points and the centre
 */
static void drawFrame (const Scope &sc)
{
    const int k = tft.SCALESZ;
    tft.drawCircleRaw (sc.cx, sc.cy, sc.r/3, RING_COLOR);
    tft.drawCircleRaw (sc.cx, sc.cy, 2*sc.r/3, RING_COLOR);
    tft.drawCircleRaw (sc.cx, sc.cy, sc.r, EDGE_COLOR);
    for (int a = 0; a < 360; a += 30) {
        float s = sinf(deg2rad(a)), c = cosf(deg2rad(a));
        int r0 = sc.r - (a % 90 ? 6 : 10);
        tft.drawLineRaw (roundf(sc.cx + r0*s), roundf(sc.cy - r0*c), roundf(sc.cx + sc.r*s), roundf(sc.cy - sc.r*c),
                        1, EDGE_COLOR);
    }
    static const char *points[] = {"N", "E", "S", "W"};
    for (int i = 0; i < 4; i++) {
        float s = sinf(deg2rad(90*i)), c = cosf(deg2rad(90*i));
        int r = sc.r - 20;
        fastText (roundf((sc.cx + r*s)/k) - getTextWidth(points[i])/2, roundf((sc.cy - r*c)/k) - 4, EDGE_COLOR, points[i]);
    }
    char str[16];
    for (int i = 1; i <= 2; i++) {
        snprintf (str, sizeof(str), "%.0f", rangeMi()*i/3.0F);
        int r = sc.r*i/3;
        fastText (roundf((sc.cx + r*0.707F)/k) + 1, roundf((sc.cy - r*0.707F)/k) - 9, RING_COLOR, str);
    }
    tft.drawLineRaw (sc.cx-4, sc.cy, sc.cx+4, sc.cy, 1, RA8875_WHITE);
    tft.drawLineRaw (sc.cx, sc.cy-4, sc.cx, sc.cy+4, 1, RA8875_WHITE);
}

/* an aircraft's symbol at raw x, y in color, by its category: heavy and jets a chevron along its
 * track (heavy larger), light a dot, rotorcraft a cross, gliders, balloons and drones a diamond,
 * surface vehicles a square, unknown a triangle; hollow if the position is from MLAT or TIS-B.
 */
static void drawSymbol (int16_t x, int16_t y, const Aircraft &ac, uint16_t color)
{
    const char *cat = ac.cat;
    bool fill = !ac.mlat;
    float t = deg2rad (ac.track < 0 ? 0 : ac.track);
    float ct = cosf(t), st = sinf(t);
    // (px, py) about the aircraft, py ahead along the track up the screen when track is north
    auto rot = [&](float px, float py, int16_t &rx, int16_t &ry) {
        rx = roundf (x + px*ct - py*st);
        ry = roundf (y + px*st + py*ct);
    };

    bool jet = (cat[0] == 'A' && cat[1] >= '3' && cat[1] <= '6') || (cat[0] == '-' && ac.speed > 250);
    if (jet) {
        float s = strcmp (cat, "A5") == 0 ? 6 : 4.5F;
        int16_t tx, ty, rx, ry, nx, ny, lx, ly;
        rot (0, -1.7F*s, tx, ty);                       // tip
        rot (s, s, rx, ry);
        rot (0, 0.35F*s, nx, ny);                       // notch at the back
        rot (-s, s, lx, ly);
        if (fill) {
            tft.fillTriangleRaw (tx, ty, rx, ry, nx, ny, color);
            tft.fillTriangleRaw (tx, ty, nx, ny, lx, ly, color);
        } else {
            tft.drawLineRaw (tx, ty, rx, ry, 1, color);
            tft.drawLineRaw (rx, ry, nx, ny, 1, color);
            tft.drawLineRaw (nx, ny, lx, ly, 1, color);
            tft.drawLineRaw (lx, ly, tx, ty, 1, color);
        }
    } else if (cat[0] == 'A' && (cat[1] == '1' || cat[1] == '2')) {
        if (fill)
            tft.fillCircleRaw (x, y, 3, color);
        else
            tft.drawCircleRaw (x, y, 3, color);
    } else if (strcmp (cat, "A7") == 0) {
        tft.drawLineRaw (x-4, y-4, x+4, y+4, 2, color);
        tft.drawLineRaw (x-4, y+4, x+4, y-4, 2, color);
    } else if (cat[0] == 'B') {
        if (fill) {
            tft.fillTriangleRaw (x, y-5, x+4, y, x-4, y, color);
            tft.fillTriangleRaw (x, y+5, x+4, y, x-4, y, color);
        } else {
            tft.drawLineRaw (x, y-5, x+4, y, 1, color);
            tft.drawLineRaw (x+4, y, x, y+5, 1, color);
            tft.drawLineRaw (x, y+5, x-4, y, 1, color);
            tft.drawLineRaw (x-4, y, x, y-5, 1, color);
        }
    } else if (cat[0] == 'C') {
        if (fill)
            tft.fillRectRaw (x-3, y-3, 7, 7, color);
        else
            tft.drawRectRaw (x-3, y-3, 7, 7, color);
    } else {
        tft.drawTriangleRaw (x, y-5, x+4, y+4, x-4, y+4, color);
    }
}

// an aircraft on the scope, and its label if it has room
typedef struct {
    int16_t x, y;                               // raw px
    float r;                                    // from DE, raw px
    const Aircraft *ac;
    uint16_t color;
    bool labeled;
    Area label;                                 // app px
    char fl[12];                                // flight level, "GND" or ""
} Shown;

/* the aircraft within range, nearest first
 */
static std::vector<Shown> locate (const Scope &sc, const Feed &feed)
{
    std::vector<Shown> shown;
    for (const Aircraft &ac : feed.aircraft) {
        Shown s;
        if (!project (sc, ac.spot, s.x, s.y))
            continue;
        s.r = hypotf (s.x - sc.cx, s.y - sc.cy);
        s.ac = &ac;
        s.color = altColor (ac.alt);
        s.labeled = false;
        s.fl[0] = '\0';
        if (ac.alt == -2)
            strcpy (s.fl, "GND");
        else if (ac.alt >= 0)
            snprintf (s.fl, sizeof(s.fl), "%03d", ac.alt/100);
        shown.push_back (s);
    }
    std::sort (shown.begin(), shown.end(), [](const Shown &a, const Shown &b) { return (a.r < b.r); });
    return (shown);
}

/* a label for each aircraft, nearest first, where one fits: right of it, left, above or below,
 * within the box and clear of the others; the labels' areas are added to taken
 */
static void placeLabels (std::vector<Shown> &shown, const SBox &box, std::vector<Area> &taken)
{
    const int k = tft.SCALESZ;
    for (Shown &s : shown) {
        int w = getTextWidth (s.ac->name), h = s.fl[0] ? 18 : 9;
        if (getTextWidth (s.fl) > w)
            w = getTextWidth (s.fl);
        int x = s.x/k, y = s.y/k;
        const Area tries[] = {{x+4, y-h/2, w, h}, {x-4-w, y-h/2, w, h}, {x-w/2, y-6-h, w, h}, {x-w/2, y+6, w, h}};
        for (const Area &a : tries) {
            if (a.x > box.x + 1 && a.x + a.w < box.x + box.w - 1 && a.y > box.y + 1
                                && a.y + a.h < box.y + box.h - 11 && clear (a, taken)) {
                s.label = a;
                s.labeled = true;
                taken.push_back (a);
                break;
            }
        }
    }
}

/* the aircraft: trails and 1-minute vectors dim, then symbols, then the labels placed for them
 */
static void drawAircraft (const Scope &sc, const std::vector<Shown> &shown)
{
    for (const Shown &s : shown) {
        uint16_t dim = dimColor (s.color);
        for (size_t i = 0; i + 1 < s.ac->trail.size(); i++) {
            int16_t tx, ty;
            if (project (sc, s.ac->trail[i], tx, ty))
                tft.drawPixelRaw (tx, ty, dim);
        }
        if (s.ac->speed > 0 && s.ac->track >= 0) {
            float r = s.ac->speed*1.852F/60/sc.km*sc.r;
            float t = deg2rad (s.ac->track);
            tft.drawLineRaw (s.x, s.y, roundf(s.x + r*sinf(t)), roundf(s.y - r*cosf(t)), 1, dim);
        }
    }
    for (const Shown &s : shown)
        drawSymbol (s.x, s.y, *s.ac, s.color);
    for (const Shown &s : shown) {
        if (!s.labeled)
            continue;
        fastText (s.label.x, s.label.y, s.color, s.ac->name);
        if (s.fl[0])
            fastText (s.label.x, s.label.y + 9, s.color, s.fl);
    }
}

/* draw the whole pane
 */
static void drawPane (const SBox &box, const Feed *feed)
{
    const int k = tft.SCALESZ;
    Scope sc;
    sc.cx = k*(box.x + box.w/2);
    sc.cy = k*(box.y + 74);
    sc.r = k*64;
    sc.km = rangeMi()*MI_KM;
    bool live = feed && time(NULL) - feed->t < FEED_STALE;     // real time, not HamClock's offset clock

    tft.holdCanvas (true);

    uint32_t t0 = millis();
    prepPlotBox (box);
    selectFontStyle (LIGHT_FONT, FAST_FONT);
    readMap();
    std::vector<Shown> shown;
    std::vector<Area> taken = {{box.x, box.y, 40, 20}, {box.x+box.w-62, box.y, 62, 20}};     // the top corners' text
    if (live) {
        shown = locate (sc, *feed);
        placeLabels (shown, box, taken);                // the aircraft's labels first: the map's give way
    }
    drawMap (sc, box, taken);
    drawFrame (sc);
    drawAircraft (sc, shown);
    int now = (int)shown.size();

    // corners: name and range top left, today's count and now top right, legend and time below
    char str[32];
    fastText (box.x+2, box.y+2, TITLE_COLOR, "ADS-B");
    snprintf (str, sizeof(str), "%d mi", rangeMi());
    fastText (box.x+2, box.y+11, TEXT_COLOR, str);
    if (feed) {
        snprintf (str, sizeof(str), "%d AC", feed->today);
        fastText (box.x+box.w-getTextWidth(str)-2, box.y+2, RA8875_WHITE, str);
    }
    if (live) {
        if (feed->nopos > 0)
            snprintf (str, sizeof(str), "%d now +%d", now, feed->nopos);
        else
            snprintf (str, sizeof(str), "%d now", now);
        fastText (box.x+box.w-getTextWidth(str)-2, box.y+11, TEXT_COLOR, str);
    }
    int16_t x = box.x + 2;
    for (unsigned i = 0; i < NARRAY(alt_bands); i++) {
        tft.fillRect (x, box.y+box.h-7, 4, 4, alt_bands[i].color);
        fastText (x+5, box.y+box.h-10, TEXT_COLOR, alt_bands[i].label);
        x += 5 + getTextWidth(alt_bands[i].label) + 3;
    }
    if (feed) {
        snprintf (str, sizeof(str), "%02d:%02dZ", hour(feed->t), minute(feed->t));
        fastText (box.x+box.w-getTextWidth(str)-2, box.y+box.h-10, TEXT_COLOR, str);
    }

    // why there are no aircraft, or why there may be fewer
    const char *msg = !feed ? "No ADS-B feed" : !live ? "Feed stopped" : feed->away ? "SDR on 2 m" : NULL;
    if (msg) {
        int16_t w = getTextWidth (msg) + 6;
        tft.fillRect (box.x + (box.w-w)/2, box.y+22, w, 11, AWAY_COLOR);
        fastText (box.x + (box.w-w)/2 + 3, box.y+24, RA8875_WHITE, msg);
    }

    tft.holdCanvas (false);

    uint32_t dt = millis() - t0;
    if (dt > 100)
        Serial.printf ("ADSB: drawing took %u ms\n", (unsigned)dt);
}

/* update the ADS-B pane. The feed is local: always true.
 */
bool updateADSB (const SBox &box)
{
    Feed feed;
    drawPane (box, readFeed (feed) ? &feed : NULL);
    return (true);
}

/* a tap within the pane below its top edge: choose the range.
 * return whether ours.
 */
bool checkADSBTouch (const SCoord &s, const SBox &box)
{
    if (s.y < box.y + PANETITLE_H)
        return (false);

    const int n = NARRAY(ranges_mi);
    MenuItem mitems[n];
    char labels[n][16];
    for (int i = 0; i < n; i++) {
        snprintf (labels[i], sizeof(labels[i]), "%d mi", ranges_mi[i]);
        mitems[i] = {MENU_1OFN, ranges_mi[i] == rangeMi(), 1, 3, labels[i], 0};
    }
    SBox menu_b = box;          // copy, not ref
    menu_b.w = 0;               // shrink to fit
    menu_b.x += 40;
    menu_b.y += 30;
    SBox ok_b;
    MenuInfo menu = {menu_b, ok_b, UF_CLOCKSOK, M_CANCELOK, 1, n, mitems};
    if (runMenu (menu)) {
        for (int i = 0; i < n; i++) {
            if (mitems[i].set) {
                range_mi = ranges_mi[i];
                NVWriteUInt8 (NV_ADSB_RANGE, range_mi);
            }
        }
    }
    scheduleNewPlot (PLOT_CH_ADSB);
    return (true);
}
