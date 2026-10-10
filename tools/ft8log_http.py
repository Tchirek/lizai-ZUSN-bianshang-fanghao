#!/usr/bin/env python3
"""Minimal read-only LAN view: /, /events, /qsos.adi. No file browser or controls."""
import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit


def tail(path):
    try:
        with path.open('rb') as f:
            f.seek(0, 2)
            offset = max(0, f.tell() - 65536)
            f.seek(offset)
            lines = f.read().decode('utf-8', 'replace').splitlines()
            return lines[1:] if offset else lines
    except FileNotFoundError:
        return []


def events(root):
    out = []
    for line in tail(root / 'auto.jsonl'):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def stamp(t):
    return datetime.fromtimestamp(t, timezone(timedelta(hours=8))).strftime('%m-%d %H:%M:%S')


def content(root, route):
    if route == '/qsos.adi':
        path = root / 'qsos.adi'
        return path.read_bytes() if path.exists() else b''
    rows = events(root)
    if route == '/':
        text = ['FT8 BG8ASN', '时间 UTC+8 | 对方 | 网格 | 发/收报告', '']
        for r in [r for r in rows if r.get('event') == 'qso'][-30:]:
            text.append(f"{stamp(r['end'])}  {r['call']}  {r['grid']}  {r['rst_sent']}/{r['rst_rcvd']}")
        if len(text) == 3:
            text.append('尚无已完成通联')
        text += ['', '收发过程：/events', 'ADIF 下载：/qsos.adi']
    elif route == '/events':
        text = [f"{stamp(r['t'])}  {r.get('event','')}  " +
                (r.get('message') or ' | '.join(d['message'] for d in r.get('decodes', [])))
                for r in rows[-50:]]
        text = text or ['尚无收发记录']
    else:
        return None
    return ('\n'.join(text) + '\n').encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlsplit(self.path).path
        body = content(self.server.records, route)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if route == '/qsos.adi':
            self.send_header('Content-Disposition', 'attachment; filename="qsos.adi"')
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--records', type=Path, default=Path.home() / 'influ' / 'ft8')
    ap.add_argument('--port', type=int, default=8073)
    a = ap.parse_args()
    server = ThreadingHTTPServer(('0.0.0.0', a.port), Handler)
    server.records = a.records
    server.serve_forever()
