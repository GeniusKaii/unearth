import contextlib
import io
import os
import unittest

from unearth.cli import main

from . import support


class TestCli(unittest.TestCase):
    def test_help_and_version(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_no_command_prints_help(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main([]), 0)
        self.assertIn("scan", buf.getvalue())

    def test_guard_refuses_output_on_source_volume(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["scan", "vol:" + os.path.splitdrive(os.getcwd())[0] + ":",
                         "-o", os.path.join(os.getcwd(), "should-not-exist")])
        self.assertEqual(code, 2)
        self.assertIn("源分区", err.getvalue())

    def test_selftest_command(self):
        workdir = support.make_workdir("cli-selftest-")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = main(["selftest", "--size", "8M", "--dir", workdir])
            self.assertEqual(code, 0, buf.getvalue())
            self.assertIn("全部通过", buf.getvalue())
        finally:
            support.clean(workdir)

    def test_bad_target(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["scan", "img:" + os.path.join(os.getcwd(), "nope.img"),
                         "-o", os.path.join(os.getcwd(), ".testtmp", "nope")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
