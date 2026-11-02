"""Verify persistent logs follow the mounted configuration directory."""

import io
import logging
import os
import tempfile
import unittest
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from unittest.mock import patch

from bluray_poster import setup_logging


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.logger = logging.Logger("logging-test")
        self.addCleanup(self.close_handlers)
        self.console = io.StringIO()

    def close_handlers(self):
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)

    def configure(self, config_dir, level="info"):
        with patch.dict(os.environ, {"CONFIG_DIR": str(config_dir)}), \
                patch("bluray_poster.logging.getLogger", return_value=self.logger), \
                patch("sys.stderr", self.console):
            setup_logging(level)
        return Path(config_dir) / "logs" / "blurayposter.log"

    def test_mounted_config_logs_contain_utf8_and_respect_level(self):
        path = self.configure(Path(self.directory.name) / "mounted-config")
        self.logger.debug("hidden debug entry")
        self.logger.info("Playback started: %s", "\u7535\u5f71")
        text = path.read_text(encoding="utf-8")
        self.assertIn("INFO [logging-test] Playback started: \u7535\u5f71", text)
        self.assertNotIn("hidden debug entry", text)
        self.assertIn("Playback started: \u7535\u5f71", self.console.getvalue())
        self.assertRegex(text, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_default_config_dir_is_relative_to_working_directory(self):
        # Avoid changing the process directory shared with other tests.
        with patch.dict(os.environ, {}, clear=True), \
                patch("bluray_poster.logging.getLogger", return_value=self.logger), \
                patch("bluray_poster.os.makedirs") as mkdir, \
                patch("bluray_poster.TimedRotatingFileHandler") as handler:
            setup_logging("info")
        mkdir.assert_called_once_with(os.path.join("config", "logs"), exist_ok=True)
        self.assertEqual(handler.call_args.args[0],
                         os.path.join("config", "logs", "blurayposter.log"))

    def test_daily_rotation_preserves_previous_log_and_keeps_seven_backups(self):
        path = self.configure(self.directory.name, "debug")
        handler = next(h for h in self.logger.handlers if isinstance(h, TimedRotatingFileHandler))
        self.assertEqual((handler.when, handler.interval, handler.backupCount),
                         ("MIDNIGHT", 86400, 7))
        self.logger.debug("before rotation")
        handler.doRollover()
        self.logger.debug("after rotation")
        self.assertIn("after rotation", path.read_text(encoding="utf-8"))
        self.assertNotIn("before rotation", path.read_text(encoding="utf-8"))
        backups = list(path.parent.glob("blurayposter.log.*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("before rotation", backups[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
