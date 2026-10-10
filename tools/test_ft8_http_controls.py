import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scheduler'))
from ft8_web import Control, serve
from ft8log_http import Handler


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gate = Control('BG8ASN','OM20',lambda s:s)
        self.stopped = threading.Event()
        self.backend = threading.Thread(target=serve,args=(str(self.root/'web.sock'),self.gate,self.stopped),daemon=True)
        self.backend.start()
        for _ in range(100):
            if (self.root/'web.sock').exists():
                break
            time.sleep(0.01)
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.records = self.root
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.stopped.set()
        self.backend.join(2)
        self.temp.cleanup()

    def post(self, action, origin=None):
        c = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        headers = {'Content-Type':'application/json'}
        if origin:
            headers['Origin'] = origin
        c.request('POST','/control',json.dumps(dict(action=action,session='page123456',message='HI')),headers)
        r = c.getresponse()
        status, body = r.status, r.read()
        c.close()
        return status, body

    def test_view_send_close_and_csrf(self):
        c = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        c.request('GET','/')
        r = c.getresponse()
        self.assertIn('一键 CQ',r.read().decode())
        self.assertIsNone(self.gate.owner)
        c.close()
        self.assertEqual(self.post('send','http://other.example')[0],403)
        self.assertIsNone(self.gate.owner)
        self.assertTrue(json.loads(self.post('send')[1])['manual'])
        self.assertFalse(json.loads(self.post('close')[1])['manual'])

    def test_stream_disconnect_releases_manual_mode(self):
        self.post('send')
        c = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        c.request('GET','/live?session=page123456')
        r = c.getresponse()
        self.assertIn(b'"manual": true',r.fp.readline())
        r.close()
        c.close()
        for _ in range(40):
            if self.gate.owner is None:
                break
            time.sleep(0.1)
        self.assertIsNone(self.gate.owner)


if __name__ == '__main__':
    unittest.main()
