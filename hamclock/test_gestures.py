import unittest
from unittest.mock import Mock

import gestures as g


def fresh():
    """A Gestures object without a Wayland connection, fed events by hand."""
    x = object.__new__(g.Gestures)
    x.display, x.queue, x.events, x.scale, x.source = Mock(), None, [], 1.0, None
    return x


class Tests(unittest.TestCase):
    def test_pinch_gives_step_ratios_and_each_pinch_starts_afresh(self):
        x = fresh()
        x._update(None, 0, 1.5, -2.0, 1.2, 0)
        x._update(None, 0, 0.0, 0.0, 1.5, 0)
        x._end(None, 2, 0, 0)
        x._update(None, 0, 0.0, 0.0, 0.8, 0)                     # the next pinch, from 1
        out = x.poll()
        self.assertEqual([e[0] for e in out], ['pinch', 'pinch', 'pinch_end', 'pinch'])
        self.assertAlmostEqual(out[0][1], 1.2)
        self.assertAlmostEqual(out[1][1], 1.25)
        self.assertAlmostEqual(out[3][1], 0.8)
        self.assertEqual(out[0][2:], (1.5, -2.0))

    def test_finger_scrolls_pan_and_wheel_clicks_zoom(self):
        x = fresh()
        x._axis_source(None, g.FINGER)
        x._axis(None, 1500, g.WlPointer.axis.vertical_scroll, 12.0)
        x._axis(None, 1500, g.WlPointer.axis.horizontal_scroll, -3.0)
        x._frame(None)
        x._axis_stop(None, 1520, g.WlPointer.axis.vertical_scroll)
        x._axis_stop(None, 1520, g.WlPointer.axis.horizontal_scroll)
        x._frame(None)
        x._axis_source(None, g.WHEEL)
        x._axis_value120(None, g.WlPointer.axis.vertical_scroll, -240)
        x._axis(None, 0, g.WlPointer.axis.vertical_scroll, -30.0)
        x._frame(None)
        self.assertEqual(x.poll(), [('scroll', 0, 12.0, 1.5), ('scroll', -3.0, 0, 1.5), ('scroll_end', 1.52),
                                    ('wheel', 2.0)])
        self.assertEqual(x.poll(), [])


if __name__ == '__main__':
    unittest.main()
