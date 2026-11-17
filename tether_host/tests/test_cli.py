import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tether_host import cli  # noqa: E402

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "tether.example.toml")


def run(*argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:   # argparse errors
            code = e.code
    return code, out.getvalue() + err.getvalue()


class Cli(unittest.TestCase):
    def test_config_option_before_and_after_the_command(self):
        for argv in (("--config", TEMPLATE, "check-config"), ("check-config", "--config", TEMPLATE),
                     ("check-config", "-c", TEMPLATE, "--log-level", "warning")):
            code, text = run(*argv)
            self.assertEqual(code, 1, (argv, text))            # the template still has placeholders...
            self.assertIn("template placeholder", text, argv)  # ...and that is what is reported, not a usage error

    def test_missing_file_is_a_clear_error(self):
        code, text = run("check-config", "--config", "does-not-exist.toml")
        self.assertEqual(code, 1)
        self.assertIn("cannot read does-not-exist.toml", text)


if __name__ == "__main__":
    unittest.main()
