import os
import re
import sys
import tomllib
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tether_host.config import ConfigError, load_config, parse_config  # noqa: E402

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "tether.example.toml")


class Template(unittest.TestCase):
    """The template that ships in the release zip must be clean: it parses, every value to fill in is a placeholder,
    nothing optional (hotspot, forwards) is switched on, and once filled in it is a valid configuration."""

    def test_unedited_template_is_refused_with_clear_messages(self):
        with self.assertRaises(ConfigError) as cm:
            load_config(TEMPLATE)
        text = "\n".join(cm.exception.problems)
        for key in ("wifi.network[0].ssid", "wifi.network[0].psk", "listener[0].target"):
            self.assertIn(key, text)
        self.assertTrue(all("placeholder" in p for p in cm.exception.problems), cm.exception.problems)

    def test_filled_in_template_is_valid_and_minimal(self):
        with open(TEMPLATE, encoding="utf-8") as fh:
            text = fh.read()
        values = {"<Wi-Fi name>": "MyNetwork", "<Wi-Fi password>": "secret-password",
                  "<API host or address on the PC's network>": "10.0.0.5"}
        for k, v in values.items():
            text = text.replace(k, v)
        self.assertNotRegex("\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")), r"<[^<>]+>")
        cfg = parse_config(tomllib.loads(text))
        self.assertIsNone(cfg.hotspot)             # optional parts stay off until the user enables them
        self.assertEqual(cfg.forwards, [])
        self.assertEqual(cfg.listeners[0].target_host, "10.0.0.5")
        self.assertEqual(cfg.host.port, "auto")

    def test_template_holds_no_real_looking_secrets(self):
        with open(TEMPLATE, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r'\s*#?\s*psk\s*=\s*"([^"]*)"', line)
                if m:
                    self.assertRegex(m.group(1), r"^<.*>$", line)


if __name__ == "__main__":
    unittest.main()
