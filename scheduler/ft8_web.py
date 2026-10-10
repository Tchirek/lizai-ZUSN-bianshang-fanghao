"""Small page-session gate. Viewing never arms it; an explicit send does."""
import json
import os
import re
import socket
import threading
import time


class Control:
    def __init__(self, call, grid, validate, clock=time.time):
        self.call, self.grid, self.validate, self.clock = call, grid[:4], validate, clock
        self.lock = threading.RLock()
        self.owner, self.seen, self.last_tx = None, 0, 0
        self.cq_tx = float('-inf')
        self.queue, self.suggestion, self.approved = None, None, None
        self.sequence = 0

    def expire(self):
        if self.owner and self.clock() - self.seen > 6:
            self.owner, self.queue, self.suggestion, self.approved = None, None, None, None

    def handle(self, req):
        session = req.get("session", "")
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", session):
            raise ValueError("invalid page session")
        action = req.get("action")
        message = None
        if action in ("cq", "send"):
            message = f"CQ {self.call} {self.grid}" if action == "cq" else req.get("message", "")
            message = self.validate(message)
        with self.lock:
            self.expire()
            if action in ("cq", "send"):
                if self.owner and self.owner != session:
                    raise ValueError("另一个页面正在手动操作")
                if self.queue:
                    raise ValueError("已有消息排队，请等待发送或关闭页面取消")
                self.owner, self.seen = (None, 0) if action == "cq" else (session, self.clock())
                self.sequence += 1
                self.queue = dict(id=self.sequence, message=message, automatic=action == "cq")
                self.suggestion, self.approved = None, None
            elif action == "approve":
                if self.owner != session or not self.suggestion or req.get("id") != self.suggestion["id"]:
                    raise ValueError("回复已更新或本页面不是手动操作页面")
                self.seen, self.approved = self.clock(), self.suggestion["id"]
            elif action == "close":
                if self.owner == session:
                    self.owner, self.queue, self.suggestion, self.approved = None, None, None, None
            elif action == "status":
                if self.owner == session:
                    self.seen = self.clock()    # kept alive by the page's HTTP stream, not a browser timer
            else:
                raise ValueError("unknown action")
            return dict(ok=True, manual=bool(self.owner), owned=self.owner == session,
                        queued=self.queue, suggestion=self.suggestion, approved=self.approved,
                        call=self.call)

    def queued(self):
        with self.lock:
            self.expire()
            return self.queue

    def recent(self, now):
        with self.lock:
            self.expire()
            return bool((self.owner and 0 < now - self.last_tx <= 240)
                        or 0 < now - self.cq_tx <= 240)

    def allow(self, message, source):
        with self.lock:
            self.expire()
            if not self.owner:
                return True
            if self.queue:
                return False
            if not self.suggestion or (self.suggestion["message"], self.suggestion["source"]) != (message, source):
                self.sequence += 1
                self.suggestion = dict(id=self.sequence, message=message, source=source)
                self.approved = None
            return self.approved == self.suggestion["id"]

    def permit(self, manual_id, message, source):
        if manual_id is None:
            return self.allow(message, source)
        with self.lock:
            self.expire()
            return bool(self.queue and self.queue["id"] == manual_id)

    def discard(self, source=None):
        with self.lock:
            if self.suggestion and (source is None or self.suggestion["source"] == source):
                self.suggestion, self.approved = None, None

    def sent(self, message, slot, manual_id=None):
        with self.lock:
            if manual_id is not None:
                self.last_tx = slot
                if self.queue and self.queue["id"] == manual_id:
                    if self.queue["automatic"]:
                        self.cq_tx = slot
                    self.queue = None
            if self.suggestion and self.suggestion["message"] == message:
                self.suggestion, self.approved = None, None


def serve(path, control, stopped):
    if os.path.exists(path):
        os.unlink(path)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        os.chmod(path, 0o600)
        server.listen(8)
        server.settimeout(1)
        while not stopped.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(2)
                try:
                    with conn.makefile("rb") as f:
                        line = f.readline(4097)
                    if len(line) > 4096:
                        raise ValueError("request too large")
                    result = control.handle(json.loads(line))
                except (ValueError, OSError, KeyError, TypeError) as e:
                    result = dict(ok=False, error=str(e))
                try:
                    conn.sendall((json.dumps(result) + "\n").encode())
                except OSError:
                    pass
