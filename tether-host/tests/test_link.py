import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tether_host import link  # noqa: E402


class Port:
    def __init__(self, device, vid, pid, product=None, serial=None):
        self.device, self.vid, self.pid, self.product, self.serial_number = device, vid, pid, product, serial
        self.description = "USB Serial Device"


PORTS = [
    Port("COM3", 0x1209, 0x0001, None, "F412FA6C1D20"),       # tether-node on Windows: no product string
    Port("COM4", 0x303A, 0x1001),                             # ESP32 in ROM download mode
    Port("COM5", 0x303A, 0x4001),                             # some other ESP32 TinyUSB gadget
    Port("COM6", 0x1A86, 0x55D3),                             # CH343 UART bridge (the board's second USB-C port)
    Port("/dev/ttyACM0", 0x1209, 0x0001, "tether-node", "F412FA6C1D21"),
]


class Discovery(unittest.TestCase):
    def find(self, *a):
        with mock.patch("serial.tools.list_ports.comports", return_value=PORTS):
            return link.find_ports(*a)

    def test_matches_by_usb_id_and_product(self):
        self.assertEqual(self.find("auto"), ["COM3", "/dev/ttyACM0"])

    def test_serial_number_selects_one_board(self):
        self.assertEqual(self.find("auto", "F412FA6C1D21"), ["/dev/ttyACM0"])

    def test_explicit_port_is_used_as_is(self):
        self.assertEqual(self.find("COM9"), ["COM9"])

    def test_download_mode_is_reported_once(self):
        link._warned.clear()
        with self.assertLogs("tether.link", "DEBUG") as cm:
            self.find("auto")
            self.find("auto")
        warnings = [m for m in cm.output if m.startswith("WARNING") and "download mode" in m]
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
