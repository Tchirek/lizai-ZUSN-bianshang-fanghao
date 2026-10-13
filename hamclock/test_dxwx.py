"""The weather pane follows a place's popup, against a stand-in for HamClock's REST port."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

import dxwx


class HamClock(ThreadingHTTPServer):
    """Panes and DX as HamClock keeps them, and the commands it was sent."""

    def __init__(self, panes):
        self.panes, self.sent, self.dx = dict(panes), [], None
        super().__init__(('127.0.0.1', 0), Handler)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url, hc = urlsplit(self.path), self.server
        args = {k: v[0] for k, v in parse_qs(url.query).items()}
        hc.sent.append((url.path, args))
        status, body = 200, ''
        if self.path.endswith('?'):                     # HamClock: "get_config.txt?" is no command
            status, body = 400, 'unknown command'
        elif url.path == '/get_config.txt':
            body = ''.join(f'Pane{n}     {" ".join(c)}\n' for n, c in sorted(hc.panes.items()))
        elif url.path == '/set_pane':
            (key, value), = args.items()
            n, choices = int(key[4:]), value.split(',')
            shown = [m for m, c in hc.panes.items() if m != n and set(c) & set(choices)]
            if shown:
                status, body = 400, f'set_pane? error: {choices[0]} already set in pane {shown[0]}'
            else:
                hc.panes[n] = choices
                body = f'Pane{n}     {" ".join(choices)}\n'
        elif url.path == '/set_newdx':
            hc.dx = float(args['lat']), float(args['lng'])
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_):
        pass


class Tests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(lambda: [os.unlink(os.path.join(self.folder, f)) for f in os.listdir(self.folder)])

    def hamclock(self, panes):
        hc = HamClock(panes)
        threading.Thread(target=hc.serve_forever, daemon=True).start()
        self.addCleanup(hc.server_close)
        self.addCleanup(hc.shutdown)                    # first: cleanups run last in, first out
        return hc

    def weather(self, hc, who):
        w = dxwx.DXWeather(who, rest=f'http://127.0.0.1:{hc.server_port}', state=os.path.join(self.folder, 'dxwx.json'))
        w.jobs.join()
        return w

    def test_rotating_pane_choices_are_read_as_set_pane_takes_them(self):
        config = 'Pane1     X-Ray\nPane3     DE_Wx Moon rotating in 40 seconds\n'
        self.assertEqual(dxwx.weather_pane(config), (3, 'DE_Wx,Moon'))
        self.assertIsNone(dxwx.weather_pane('Pane3     Moon DE_Wx\n'))   # DE_Wx not showing now

    def test_the_pane_shows_the_place_while_open_and_is_given_back_whole(self):
        hc = self.hamclock({1: ['X-Ray'], 3: ['DE_Wx', 'Moon']})
        w = self.weather(hc, 'native')
        w.show('native', 30.1234, 105.5678)
        w.jobs.join()
        self.assertEqual(hc.panes[3], ['DX_Wx'])
        self.assertEqual(hc.dx, (30.1234, 105.5678))
        # the pane turns before DX moves: HamClock never borrows another pane for DX's weather
        self.assertLess([p for p, _ in hc.sent].index('/set_pane'), [p for p, _ in hc.sent].index('/set_newdx'))
        w.show('native', 30.2, 105.6)                                   # another place: the pane stays
        w.close('native')
        w.jobs.join()
        self.assertEqual(hc.panes[3], ['DE_Wx', 'Moon'])
        self.assertEqual([p for p, _ in hc.sent].count('/set_pane'), 2)

    def test_the_last_popup_opened_owns_the_pane(self):
        hc = self.hamclock({3: ['DE_Wx']})
        native, web = self.weather(hc, 'native'), self.weather(hc, 'web')
        native.show('native', 30, 105)
        native.jobs.join()
        web.show('web:a', 31, 106)
        web.jobs.join()
        native.close('native')                                          # not its pane any more
        native.jobs.join()
        self.assertEqual(hc.panes[3], ['DX_Wx'])
        web.close('web:a')
        web.jobs.join()
        self.assertEqual(hc.panes[3], ['DE_Wx'])

    def test_a_display_starting_again_gives_back_what_its_last_run_held(self):
        hc = self.hamclock({3: ['DE_Wx']})
        web = self.weather(hc, 'web')
        web.show('web:gone', 30, 105)
        web.jobs.join()
        self.weather(hc, 'native')                                      # someone else's: left alone
        self.assertEqual(hc.panes[3], ['DX_Wx'])
        self.weather(hc, 'web')                                         # the web server again
        self.assertEqual(hc.panes[3], ['DE_Wx'])

    def test_dx_weather_already_on_show_moves_dx_only(self):
        hc = self.hamclock({1: ['DX_Wx'], 3: ['DE_Wx']})
        w = self.weather(hc, 'native')
        w.show('native', 30, 105)
        w.close('native')
        w.jobs.join()
        self.assertEqual(hc.panes, {1: ['DX_Wx'], 3: ['DE_Wx']})
        self.assertEqual(hc.dx, (30, 105))


if __name__ == '__main__':
    unittest.main()
