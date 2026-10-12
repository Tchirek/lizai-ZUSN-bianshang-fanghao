#!/usr/bin/env python3
"""The station's FT8 page: the log, one-click CQ, manual sends and session-scoped reply
approval, and when the SDR may leave 1090 MHz today.

It serves the whole LAN. Once ~/influ/private/ft8web.auth exists (`ft8log_http.py passwd`
makes it), every request needs HTTP Basic Auth: the browser asks once and remembers. Over plain
HTTP that keeps other people on the LAN from transmitting; it is no protection beyond the LAN.

The outlook is read from the scheduler's own models and state: arrivals the hazard model
expects each hour, the half-hours the training model protects, today's budget, the
scheduler's latest decision and the skeds agreed over the chat.
"""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import getpass
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import socket
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOME = Path.home() / 'influ'
AUTH = HOME / 'private' / 'ft8web.auth'
TZ = timezone(timedelta(hours=8))

PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>FT8</title>
<style>body{max-width:680px;margin:32px auto;padding:0 16px;font:15px system-ui;line-height:1.5}button,input{font:inherit;padding:7px 10px}button{cursor:pointer}input{width: min(60%,380px)}pre{white-space:pre-wrap;font-size:13px}small{color:#666}#error{color:#b22}#draft{padding:12px;background:#f3f4f5}
#hours{display:grid;grid-template-columns:repeat(48,1fr);gap:1px;height:16px;margin:6px 0 2px}#hours i{background:#3a4a5c}#hours i.p{background:#c0392b}#hours i.s{background:#d4a017}#hours i.now{outline:2px solid #111;outline-offset:1px}#axis{display:flex;justify-content:space-between;font-size:12px;color:#666}</style>
<h3>FT8 · @CALL@</h3><p id="mode">连接中…</p>
<button id="cq" onclick="act('cq')">一键 CQ</button>
<form onsubmit="event.preventDefault();act('send',{message:document.querySelector('input').value})">
<p><input aria-label="发送内容" maxlength="37"> <button id="send">发送</button></p></form>
<p id="queue"></p><p id="error" role="status"></p>
<div id="draft" hidden><div>待批准：<strong id="message"></strong> <small id="source"></small></div>
<button id="approve" onclick="act('approve',{id:draft.id})">批准并发送</button></div>
<h4>今天的收听时机</h4><div id="hours"></div><div id="axis"><span>0</span><span>6</span><span>12</span><span>18</span><span>24</span></div>
<p id="gate"></p>
<p><a href="/qsos.adi">下载 ADIF</a> · <a href="/events" target="_blank">收发全文</a></p>
<pre id="records">暂无记录</pre>
<script>
const session=Date.now().toString(36)+Math.random().toString(36).slice(2);let draft=null;
async function act(action,extra={}){
 try{const r=await fetch('/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,session,...extra})});
 const s=await r.json();if(!s.ok)throw Error(s.error);document.querySelector('#error').textContent='';render(s);
 }catch(e){document.querySelector('#error').textContent=e.message}}
function outlook(o){
 const max=Math.max(1,...o.cells.map(c=>c.lam||0));
 document.querySelector('#hours').innerHTML=o.cells.map((c,i)=>'<i class="'+(c.sked?'s':c.protected?'p':'')+(i===o.slot?' now':'')+'" style="opacity:'+(c.sked?1:0.2+0.8*(c.lam||0)/max).toFixed(2)+'" title="'+String(i>>1).padStart(2,'0')+(i%2?':30':':00')+(c.lam==null?'':' 预计 '+c.lam.toFixed(0)+' 架/时')+(c.protected?' · 训练保护':'')+(c.sked?' · 约定守听':'')+'"></i>').join('');
 document.querySelector('#gate').textContent=o.text;
}
function render(s){
 document.querySelector('#mode').textContent=!s.ok?'控制器暂不可用':s.manual?(s.owned?'手动会话 · 自动回复等待批准':'另一页面正在手动操作'):'全自动';
 const blocked=!s.ok||(s.manual&&!s.owned);for(const id of ['cq','send'])document.getElementById(id).disabled=blocked||!!s.queued;
 document.querySelector('#queue').textContent=s.queued?'排队：'+s.queued.message:'';
 draft=s.suggestion||null;document.querySelector('#draft').hidden=!draft;
 if(draft){document.querySelector('#message').textContent=draft.message;document.querySelector('#source').textContent=draft.source;
 document.querySelector('#approve').disabled=!s.owned||s.approved===draft.id;document.querySelector('#approve').textContent=s.approved===draft.id?'已批准，等待时隙':'批准并发送';}
 if(s.records!==undefined)document.querySelector('#records').textContent=s.records;
 if(s.outlook)outlook(s.outlook);
}
const live=new EventSource('/live?session='+encodeURIComponent(session));live.onmessage=e=>render(JSON.parse(e.data));
live.onerror=()=>{document.querySelector('#mode').textContent='连接恢复中…'};
function leave(){live.close();navigator.sendBeacon('/control',new Blob([JSON.stringify({action:'close',session})],{type:'application/json'}));}
window.addEventListener('pagehide',leave);window.addEventListener('beforeunload',leave);
</script></html>'''

def station_call():
    try:
        with open(HOME / 'station.json') as f:
            return json.load(f)['call']
    except (OSError, ValueError, KeyError):
        return ''


def hash_password(password, salt=None, rounds=200_000):
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, rounds)
    return f'pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}'


class Login:
    """HTTP Basic Auth from a one-line file `user:pbkdf2_sha256$rounds$salt$hash`; while the
    file is absent every request passes, as before it existed."""

    def __init__(self, path):
        self.path, self.stamp, self.user, self.stored, self.good = Path(path), None, None, None, set()

    def required(self):
        try:
            stamp = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self.stamp, self.good = None, set()
            return False
        if stamp != self.stamp:
            self.user, _, self.stored = self.path.read_text().strip().partition(':')
            self.stamp, self.good = stamp, set()
        return True

    def allows(self, header):
        if not self.required():
            return True
        if not header or not header.startswith('Basic '):
            return False
        key = hashlib.sha256(header.encode()).digest()
        if key in self.good:                         # checked once; pbkdf2 is slow on purpose
            return True
        try:
            user, _, password = base64.b64decode(header[6:], validate=True).decode().partition(':')
            _, rounds, salt, digest = self.stored.split('$')
        except ValueError:
            return False
        same_user = hmac.compare_digest(user.encode(), self.user.encode())
        same_password = hmac.compare_digest(hash_password(password, bytes.fromhex(salt), int(rounds)),
                                            self.stored)
        if same_user and same_password:
            self.good.add(key)
        return same_user and same_password


def control(root, request):
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(2)
            s.connect(str(root / 'web.sock'))
            s.sendall((json.dumps(request) + '\n').encode())
            with s.makefile('rb') as f:
                return json.loads(f.readline(8192))
    except (OSError, ValueError):
        return dict(ok=False, error='控制器暂不可用')


def tail(path, size=65536):
    try:
        with path.open('rb') as f:
            f.seek(0, 2)
            offset = max(0, f.tell() - size)
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
    return datetime.fromtimestamp(t, TZ).strftime('%m-%d %H:%M:%S')


_files = {}


def read_json(path):
    """A small JSON file, read again only when it changes; None if missing or unreadable."""
    try:
        mtime = path.stat().st_mtime_ns
        if _files.get(path, (None,))[0] != mtime:
            _files[path] = (mtime, json.loads(path.read_text()))
        return _files[path][1]
    except (OSError, ValueError):
        return None


def limits(home):
    """The scheduler's daily blind budget and hourly window cap, from its arguments."""
    try:
        args = (home / 'scheduler' / 'scheduler.env').read_text()
    except OSError:
        args = ''
    blind = re.search(r'--day-blind\s+([\d.]+)', args)
    per_hour = re.search(r'--max-per-hour\s+(\d+)', args)
    return float(blind[1]) if blind else 1800.0, int(per_hour[1]) if per_hour else 6


def outlook(home, now=None):
    """Today's half-hours as the scheduler's models see them, its latest decision, and the skeds
    not yet over (ft8_sked.py) with the spans they are kept for."""
    now = now or time.time()
    local = datetime.fromtimestamp(now, TZ)
    hazard = read_json(home / 'model' / 'hazard.json') or {}
    training = read_json(home / 'model' / 'training.json') or {}
    budget = read_json(home / 'sched' / 'budget.json') or {}
    lam = hazard.get('lam_mean_per_h') or [None] * 24
    protected = set(training.get('protected_bins') or [])
    skeds = [s for s in (read_json(home / 'ft8' / 'skeds.json') or {}).get('skeds', []) if s['end'] > now]
    slot = local.hour * 2 + local.minute // 30
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    cells = [dict(lam=lam[i // 2], protected=i in protected,
                  sked=any(s['start'] < midnight + 1800 * (i + 1) and midnight + 1800 * i < s['end'] for s in skeds))
             for i in range(48)]
    day_blind, per_hour = limits(home)
    today = budget.get('day') == local.strftime('%Y-%m-%d')
    used = budget.get('blind', 0.0) if today else 0.0
    windows = (budget.get('per_hour') or {}).get(str(local.hour), 0) if today else 0
    lines = [f'今天已切出 {used:.0f} / {day_blind:.0f} 秒，本小时 {windows} / {per_hour} 个窗口。']
    last = None
    for line in reversed(tail(home / 'sched' / f'decisions-{local:%Y%m%d}.jsonl', 8192)):
        try:
            last = json.loads(line)
            break
        except ValueError:
            continue
    if last:
        when = datetime.fromtimestamp(last['t'], TZ).strftime('%H:%M:%S')
        lines.append(f"{when} 的判定：{'切到 2 米收听' if last.get('approve') else '留在 1090'}。")
    for s in skeds:
        at = datetime.fromtimestamp(s['t'], TZ)
        lines.append(f"约定 {s['call']}：{at:%m-%d %H:%M}（{datetime.fromtimestamp(s['t'], timezone.utc):%H:%M}Z），"
                     + ('已通联。' if s.get('worked') else
                        f"{datetime.fromtimestamp(s['start'], TZ):%H:%M}–{datetime.fromtimestamp(s['end'], TZ):%H:%M} 守在 2 米。"))
    return dict(slot=slot, cells=cells, text=' '.join(lines))


def content(root, route):
    if route == '/qsos.adi':
        path = root / 'qsos.adi'
        return path.read_bytes() if path.exists() else b''
    rows = events(root)
    if route == '/':
        text = [f'FT8 {station_call()}'.strip(), '时间 UTC+8 | 对方 | 网格 | 发/收报告', '']
        for r in [r for r in rows if r.get('event') == 'qso'][-30:]:
            text.append(f"{stamp(r['end'])}  {r['call']}  {r['grid']}  {r['rst_sent']}/{r['rst_rcvd']}")
        if len(text) == 3:
            text.append('尚无已完成通联')
    elif route == '/events':
        text = [f"{stamp(r['t'])}  {r.get('event','')}  " +
                (r.get('message') or ' | '.join(d['message'] for d in r.get('decodes', [])))
                for r in rows[-50:]]
        text = text or ['尚无收发记录']
    else:
        return None
    return ('\n'.join(text) + '\n').encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    def signed_in(self):
        login = getattr(self.server, 'login', None)
        if login is None or login.allows(self.headers.get('Authorization')):
            return True
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="FT8", charset="UTF-8"')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    def do_GET(self):
        if not self.signed_in():
            return
        route = urlsplit(self.path).path
        if route == '/live':
            session = parse_qs(urlsplit(self.path).query).get('session', [''])[0]
            home = getattr(self.server, 'home', HOME)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                while True:
                    state = control(self.server.records, dict(action='status', session=session))
                    state['records'] = content(self.server.records, '/').decode() + '\n' + content(self.server.records, '/events').decode()
                    state['outlook'] = outlook(home)
                    self.wfile.write(('data: ' + json.dumps(state) + '\n\n').encode())
                    self.wfile.flush()
                    time.sleep(1)
            except (OSError, ConnectionError):
                pass
            finally:
                control(self.server.records, dict(action='close', session=session))
            return
        if route == '/':
            body = PAGE.replace('@CALL@', station_call()).encode('utf-8')
        else:
            body = content(self.server.records, route)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        if route == '/':
            self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Type', ('text/html' if route == '/' else 'text/plain') + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if route == '/qsos.adi':
            self.send_header('Content-Disposition', 'attachment; filename="qsos.adi"')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.signed_in():
            return
        origin = self.headers.get('Origin')
        if (urlsplit(self.path).path != '/control' or
                self.headers.get('Content-Type', '').split(';')[0] != 'application/json' or
                (origin and urlsplit(origin).netloc != self.headers.get('Host'))):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('request size')
            req = json.loads(self.rfile.read(length))
            if req.get('action') not in ('cq', 'send', 'approve', 'close'):
                raise ValueError('action')
            result = control(self.server.records, req)
        except (ValueError, TypeError, AttributeError):
            result = dict(ok=False, error='请求格式错误')
        body = json.dumps(result).encode()
        self.send_response(200 if result.get('ok') else 400)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def set_password(path, user):
    first = getpass.getpass(f'{user} 的口令：')
    if not first or first != getpass.getpass('再输一遍：'):
        raise SystemExit('两次不一致，或为空；没有改动。')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(f'{user}:{hash_password(first)}\n')
    print(f'已写入 {path}；下一个请求起需要登录。删掉这个文件即取消。')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', nargs='?', choices=['serve', 'passwd'], default='serve',
                    help='serve the page (default), or set its login')
    ap.add_argument('--records', type=Path, default=HOME / 'ft8')
    ap.add_argument('--port', type=int, default=8073)
    ap.add_argument('--auth', type=Path, default=AUTH)
    ap.add_argument('--user', default='ft8', help='login name for passwd')
    a = ap.parse_args()
    if a.cmd == 'passwd':
        set_password(a.auth, a.user)
    else:
        server = ThreadingHTTPServer(('0.0.0.0', a.port), Handler)
        server.records, server.home, server.login = a.records, HOME, Login(a.auth)
        server.serve_forever()
