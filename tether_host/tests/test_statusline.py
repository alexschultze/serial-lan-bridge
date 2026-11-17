import io
import unittest

from tether_host import protocol as P
from tether_host import statusline as S


class Stub:
    link_up = True
    node_status = {"state": "running", "ip": "192.168.1.5", "rssi": -50}
    channels = {1: object(), 2: object()}
    rx_bytes = 3 * 1024 * 1024
    tx_bytes = 512


class StatusLineTests(unittest.TestCase):
    def test_rate_and_size(self):
        self.assertEqual(S.rate(10), "10 B/s")
        self.assertEqual(S.rate(2048), "2.0 KB/s")
        self.assertEqual(S.rate(5 * 1024 * 1024), "5.0 MB/s")
        self.assertEqual(S.size(512), "512 B")
        self.assertEqual(S.size(3 * 1024 * 1024), "3.0 MB")

    def test_render(self):
        text = S.render(Stub(), 2048, 10)
        self.assertIn("link up", text)
        self.assertIn("node running", text)
        self.assertIn("192.168.1.5 -50 dBm", text)
        self.assertIn("connections 2/%d" % P.MAX_CHANNELS, text)
        self.assertIn("from node 2.0 KB/s  to node 10 B/s", text)

    def test_render_link_down(self):
        s = Stub()
        s.link_up = False
        self.assertIn("DOWN", S.render(s, 0, 0))
        self.assertNotIn("node running", S.render(s, 0, 0))

    def test_draw_pins_last_row_and_close_releases_it(self):
        out = io.StringIO()
        line = S.StatusLine(Stub(), out)
        line.draw(0, 0)
        text = out.getvalue()
        self.assertIn("\x1b[1;", text)      # scroll region without the last row
        self.assertIn("link up", text)
        out.truncate(0)
        line.close()
        self.assertIn("\x1b[r", out.getvalue())


if __name__ == "__main__":
    unittest.main()
