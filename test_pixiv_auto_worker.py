import tempfile
import unittest
import zipfile
from pathlib import Path

import requests
from pixivpy3.utils import PixivError

from pixiv_auto_worker import (
    DEFAULT_CONFIG,
    classify_error,
    deep_merge,
    network_config,
    safe_extract_zip,
)


class NetworkErrorClassificationTests(unittest.TestCase):
    def test_classifies_requests_errors_as_network(self) -> None:
        error = requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")
        self.assertEqual(classify_error(error), "network")

    def test_classifies_pixiv_wrapped_requests_errors_as_network(self) -> None:
        error = PixivError("requests POST https://oauth.secure.pixiv.net/auth/token error: Max retries exceeded")
        self.assertEqual(classify_error(error), "network")

    def test_classifies_invalid_grant_as_token(self) -> None:
        error = PixivError("invalid_grant")
        self.assertEqual(classify_error(error), "token")


class CompatibilityTests(unittest.TestCase):
    def test_old_config_gets_network_defaults(self) -> None:
        config = deep_merge(DEFAULT_CONFIG, {"web": {"port": 18081}})
        self.assertEqual(config["web"]["port"], 18081)
        self.assertGreaterEqual(network_config(config)["api_retries"], 1)


class SafeZipTests(unittest.TestCase):
    def test_extracts_normal_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "ok.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("frames/000001.jpg", b"data")
            out_dir = root / "out"
            out_dir.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                safe_extract_zip(archive, out_dir)
            self.assertEqual((out_dir / "frames" / "000001.jpg").read_bytes(), b"data")

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "bad.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.txt", b"nope")
            out_dir = root / "out"
            out_dir.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaises(RuntimeError):
                    safe_extract_zip(archive, out_dir)


if __name__ == "__main__":
    unittest.main()
