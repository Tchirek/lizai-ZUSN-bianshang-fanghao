#!/usr/bin/env python3
"""Small FT8 page: logs, manual sends and session-scoped reply approval."""
import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>FT8</title>
<style>body{max-width:680px;margin:32px auto;padding:0 16px;font:15px system-ui;line-height:1.5}button,input{font:inherit;padding:7px 10px}button{cursor:pointer}input{width: min(60%,380px)}pre{white-space:pre-wrap;font-size:13px}small{color:#666}#error{color:#b22}#draft{padding:12px;background:#f3f4f5}</style>
<h3>FT8 · BG8ASN</h3><p id="mode">连接中…</p>
<button id="cq" onclick="act('cq')">一键 CQ</button>
<form onsubmit="event.preventDefault();act('send',{message:document.querySelector('input').value})">
<p><input aria-label="发送内容" maxlength="37" placeholder="HOW R U? / 标准 FT8 消息"> <button id="send">发送</button></p></form>
<small>自由文本最多 13 字符。仅打开页面保持全自动；点击 CQ 或发送后，自动回复需批准。关闭页面恢复全自动。</small>
<p id="queue"></p><p id="error" role="status"></p>
<div id="draft" hidden><div>待批准：<strong id="message"></strong> <small id="source"></small></div>
<button id="approve" onclick="act('approve',{id:draft.id})">批准并发送</button></div>
<p><a href="/qsos.adi">下载 ADIF</a> · <a href="/events" target="_blank">收发全文</a></p>
<pre id="records">暂无记录</pre>
<script>
const session=Date.now().toString(36)+Math.random().toString(36).slice(2);let draft=null;
async function act(action,extra={}){
 try{const r=await fetch('/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,session,...extra})});
 const s=await r.json();if(!s.ok)throw Error(s.error);document.querySelector('#error').textContent='';render(s);
 }catch(e){document.querySelector('#error').textContent=e.message}}
function render(s){
 document.querySelector('#mode').textContent=!s.ok?'控制器暂不可用':s.manual?(s.owned?'手动会话 · 自动回复等待批准':'另一页面正在手动操作'):'全自动';
 const blocked=!s.ok||(s.manual&&!s.owned);for(const id of ['cq','send'])document.getElementById(id).disabled=blocked||!!s.queued;
 document.querySelector('#queue').textContent=s.queued?'已排队：'+s.queued.message+'（下个可用 FT8 时隙发送）':'';
 draft=s.suggestion||null;document.querySelector('#draft').hidden=!draft;
 if(draft){document.querySelector('#message').textContent=draft.message;document.querySelector('#source').textContent=draft.source;
 document.querySelector('#approve').disabled=!s.owned||s.approved===draft.id;document.querySelector('#approve').textContent=s.approved===draft.id?'已批准，等待时隙':'批准并发送';}
 if(s.records!==undefined)document.querySelector('#records').textContent=s.records;
}
const live=new EventSource('/live?session='+encodeURIComponent(session));live.onmessage=e=>render(JSON.parse(e.data));
live.onerror=()=>{document.querySelector('#mode').textContent='连接恢复中…'};
function leave(){live.close();navigator.sendBeacon('/control',new Blob([JSON.stringify({action:'close',session})],{type:'application/json'}));}
window.addEventListener('pagehide',leave);window.addEventListener('beforeunload',leave);
</script></html>'''


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
        if route == '/live':
            session = parse_qs(urlsplit(self.path).query).get('session', [''])[0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                while True:
                    state = control(self.server.records, dict(action='status', session=session))
                    state['records'] = content(self.server.records, '/').decode() + '\n' + content(self.server.records, '/events').decode()
                    self.wfile.write(('data: ' + json.dumps(state) + '\n\n').encode())
                    self.wfile.flush()
                    time.sleep(1)
            except (OSError, ConnectionError):
                pass
            finally:
                control(self.server.records, dict(action='close', session=session))
            return
        if route == '/':
            body = PAGE.encode('utf-8')
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


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--records', type=Path, default=Path.home() / 'influ' / 'ft8')
    ap.add_argument('--port', type=int, default=8073)
    a = ap.parse_args()
    server = ThreadingHTTPServer(('0.0.0.0', a.port), Handler)
    server.records = a.records
    server.serve_forever()
