// HamClock's frame as its 1600x960 build draws it (ArduinoLib/Adafruit_RA8875): app coordinates
// are 800x480, each two raw px; colours go through RGB565; text is its own glyph tables (GFX
// fonts, data/glyphs.json), a raw px lit wherever a glyph's bit is set, from the cursor.
export const SCALE = 2, WIDTH = 1600, HEIGHT = 960;

const fonts = {};
export const bits = text => Uint8Array.from(atob(text), ch => ch.charCodeAt(0));
// data/glyphs.json: {font: {char: [x offset, y offset, advance, width, height, bits (base64)]}}
export function useFonts(data) {
  for (const [name, table] of Object.entries(data))
    fonts[name] = new Map(Object.entries(table).map(([c, [xo, yo, adv, w, h, b]]) => [c, {xo, yo, adv, w, h, bits: bits(b)}]));
}

// RGB565 as HamClock stores a colour, and the 888 the frame shows it as
export function rgb565(r, g, b) {
  return ((r & 0xf8) << 8) | ((g & 0xfc) << 3) | (b >> 3);
}
function pixel(c16) {
  const r = c16 >> 11 & 31, g = c16 >> 5 & 63, b = c16 & 31;
  return (255 << 24 | (b << 3 | b >> 2) << 16 | (g << 2 | g >> 4) << 8 | (r << 3 | r >> 2)) >>> 0;
}
export const C = {
  BLACK: rgb565(0, 0, 0), WHITE: rgb565(255, 255, 255), RED: rgb565(255, 0, 0), GREEN: rgb565(0, 255, 0),
  BLUE: rgb565(0, 0, 255), CYAN: rgb565(0, 255, 255), MAGENTA: rgb565(255, 0, 255), YELLOW: rgb565(255, 255, 0),
  GRAY: rgb565(140, 140, 140), BRGRAY: rgb565(200, 200, 200), DE: rgb565(255, 125, 0),
};

export class Tft {
  constructor(canvas) {
    this.canvas = canvas;
    this.context = canvas.getContext('2d');
    this.image = this.context.createImageData(WIDTH, HEIGHT);
    this.px = new Uint32Array(this.image.data.buffer);
    this.px.fill(pixel(C.BLACK));
    this.font = 'fast';
    this.colour = pixel(C.WHITE);
    this.x = this.y = 0;
  }
  show() { this.context.putImageData(this.image, 0, 0); }

  plot(x, y, c) { x |= 0; y |= 0; if (x >= 0 && x < WIDTH && y >= 0 && y < HEIGHT) this.px[y * WIDTH + x] = c; }
  fillRectRaw(x, y, w, h, c16) {
    x = Math.round(x); y = Math.round(y);
    const c = pixel(c16), x0 = Math.max(0, x), x1 = Math.min(WIDTH, x + w);
    for (let row = Math.max(0, y); row < Math.min(HEIGHT, y + h); row++) if (x1 > x0) this.px.fill(c, row * WIDTH + x0, row * WIDTH + x1);
  }
  fillRect(x, y, w, h, c16) { this.fillRectRaw(Math.trunc(x) * SCALE, Math.trunc(y) * SCALE, Math.max(1, Math.trunc(w)) * SCALE, Math.max(1, Math.trunc(h)) * SCALE, c16); }
  // Bresenham; thick lines as that many side by side across the line's minor direction
  lineRaw(x0, y0, x1, y1, thick, c16) {
    x0 = Math.round(x0); y0 = Math.round(y0); x1 = Math.round(x1); y1 = Math.round(y1);
    const c = pixel(c16), steep = Math.abs(y1 - y0) > Math.abs(x1 - x0);
    for (let k = 0; k < Math.max(1, thick); k++) {
      const o = k - Math.floor((thick - 1) / 2);
      let ax = x0 + (steep ? o : 0), ay = y0 + (steep ? 0 : o);
      const bx = x1 + (steep ? o : 0), by = y1 + (steep ? 0 : o);
      const dx = Math.abs(bx - ax), dy = -Math.abs(by - ay), sx = ax < bx ? 1 : -1, sy = ay < by ? 1 : -1;
      let err = dx + dy;
      for (;;) {
        this.plot(ax, ay, c);
        if (ax === bx && ay === by) break;
        const e2 = 2 * err;
        if (e2 >= dy) { err += dy; ax += sx; }
        if (e2 <= dx) { err += dx; ay += sy; }
      }
    }
  }
  line(x0, y0, x1, y1, c16, thick = 1) { this.lineRaw(x0 * SCALE, y0 * SCALE, x1 * SCALE, y1 * SCALE, thick * SCALE > 2 ? thick * SCALE : 1, c16); }
  drawRectRaw(x, y, w, h, c16) {
    this.lineRaw(x, y, x + w, y, 1, c16); this.lineRaw(x + w, y, x + w, y + h, 1, c16);
    this.lineRaw(x + w, y + h, x, y + h, 1, c16); this.lineRaw(x, y + h, x, y, 1, c16);
  }
  drawRect(x, y, w, h, c16) { this.drawRectRaw(x * SCALE, y * SCALE, (Math.max(1, w) - 1) * SCALE, (Math.max(1, h) - 1) * SCALE, c16); }
  // circles as plotDrawCircle and plotFillCircle scan them: whole px within half a px of the radius
  circleRaw(x0, y0, r, c16, fill = false) {
    const c = pixel(c16), inner = 4 * r * (r - 1) + 1, outer = 4 * r * (r + 1) + 1;
    for (let dy = -2 * r; dy <= 2 * r; dy += 2) for (let dx = -2 * r; dx <= 2 * r; dx += 2) {
      const d = dx * dx + dy * dy;
      if (d <= outer && (fill || d >= inner)) this.plot(x0 + dx / 2, y0 + dy / 2, c);
    }
  }
  triangleRaw(x0, y0, x1, y1, x2, y2, c16) { this.lineRaw(x0, y0, x1, y1, 1, c16); this.lineRaw(x1, y1, x2, y2, 1, c16); this.lineRaw(x2, y2, x0, y0, 1, c16); }
  fillTriangleRaw(x0, y0, x1, y1, x2, y2, c16) {
    let p = [[x0, y0], [x1, y1], [x2, y2]].map(([x, y]) => [Math.round(x), Math.round(y)]).sort((a, b) => a[1] - b[1]);
    const [[ax, ay], [bx, by], [cx, cy]] = p;
    if (by !== ay && cy !== ay) for (let y = ay; y < by; y++)
      this.lineRaw(ax + (y - ay) * (bx - ax) / (by - ay), y, ax + (y - ay) * (cx - ax) / (cy - ay), y, 1, c16);
    if (cy !== by && cy !== ay) for (let y = by; y <= cy; y++)
      this.lineRaw(bx + (y - by) * (cx - bx) / (cy - by), y, ax + (y - ay) * (cx - ax) / (cy - ay), y, 1, c16);
  }

  // text: setFont(fast|small|smallBold|large|song), the cursor in app px, print as plotChar does
  setFont(name) { this.font = name; }
  setColour(c16) { this.colour = pixel(c16); }
  setCursor(x, y) { this.x = Math.trunc(x) * SCALE; this.y = Math.trunc(y) * SCALE; }    // app px are whole, as uint16_t
  setCursorRaw(x, y) { this.x = x; this.y = y; }
  glyph(ch) { return fonts[this.font]?.get(ch) ?? (this.font === 'fast' ? fonts.song?.get(ch) : null); }
  print(text) {
    for (const ch of String(text)) {
      const g = this.glyph(ch);
      if (!g) continue;
      for (let i = 0; i < g.w * g.h; i++) if (g.bits[i >> 3] & 0x80 >> (i & 7))
        this.plot(this.x + g.xo + i % g.w, this.y + g.yo + Math.floor(i / g.w), this.colour);
      this.x += g.adv;
    }
  }
  rawWidth(text) { let w = 0; for (const ch of String(text)) w += this.glyph(ch)?.adv ?? 0; return w; }
  textWidth(text) { return Math.floor(this.rawWidth(text) / SCALE); }
  // getTextBounds: width and height in app px of what text's glyphs cover from the baseline
  textBounds(text) {
    let lo = 0, hi = 0;
    for (const ch of String(text)) { const g = this.glyph(ch); if (g) { lo = Math.min(lo, g.yo); hi = Math.max(hi, g.yo + g.h); } }
    return {w: this.textWidth(text), h: Math.floor((hi - lo) / SCALE)};
  }
}
