"""Tests for scripts/cdd_download.py against a local mock of the site's download API.

Run: python -m unittest discover -s tests
Nothing is sent to the real site.
"""

import gzip
import io
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cdd_download as dl  # noqa: E402

ANSWERS = {"name": "Test Reader", "email": "reader@example.org", "institution": "Example University",
           "role": "Faculty", "use": "2", "use_detail": "", "heard": "", "news": False, "agree": True}


def package_bytes():
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb") as g:
        g.write(json.dumps({"accession_number": "x", "model_output": {}}).encode() + b"\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("raw_extractions.jsonl.gz", raw.getvalue())
        z.writestr("README.md", "readme")
        z.writestr("LICENSE", "license")
    return buf.getvalue()


class Mock(BaseHTTPRequestHandler):
    mode = "ok"
    bodies = []
    package = package_bytes()

    def log_message(self, *args):
        pass

    def reply(self, status, body, headers=()):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        assert self.path == "/api/download/request"
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        Mock.bodies.append(body)
        if Mock.mode == "400":
            return self.reply(400, {"error": "Choose a role."})
        if Mock.mode == "429":
            return self.reply(429, {"error": "Too many requests. Try again later."}, [("retry-after", "600")])
        if Mock.mode == "503":
            return self.reply(503, {"error": "Downloads are temporarily unavailable. Please try again later."})
        port = self.server.server_address[1]
        self.reply(200, {"ok": True, "delivery": "direct", "expiresAt": "2099-01-01T00:00:00.000Z",
                         "downloads": [{"filename": "cdd_raw_v1.0.0.zip",
                                        "url": f"http://127.0.0.1:{port}/v1.0.0/cdd_raw_v1.0.0.zip?X-Amz-Signature=abc"}]})

    def do_GET(self):
        self.send_response(200)
        self.send_header("content-length", str(len(self.package)))
        self.end_headers()
        self.wfile.write(self.package)


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Mock.mode, Mock.bodies = "ok", []
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache"

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_sends_contract_body_and_unpacks(self):
        raw = dl.get_raw(self.cache, answers=ANSWERS, base=self.base)
        self.assertTrue(raw.exists())
        body = Mock.bodies[0]
        self.assertEqual(body, {"name": "Test Reader", "email": "reader@example.org", "institution": "Example University",
                                "use_detail": "", "heard": "", "role": "faculty", "use": "teaching", "news": False,
                                "agree": True, "delivery": "direct", "client": "colab"})
        self.assertFalse((self.cache / "cdd_raw_v1.0.0.zip").exists())     # zip removed after unpacking
        self.assertTrue((self.cache / "raw" / "LICENSE").exists())

    def test_second_run_uses_cache_without_request(self):
        dl.get_raw(self.cache, answers=ANSWERS, base=self.base)
        dl.get_raw(self.cache, answers=None, base=self.base, ask=lambda: self.fail("asked again"))
        self.assertEqual(len(Mock.bodies), 1)

    def test_valid_earlier_link_is_reused(self):
        dl.get_raw(self.cache, answers=ANSWERS, base=self.base)
        (self.cache / "raw" / ".checked").unlink()                          # pretend the download was lost
        dl.get_raw(self.cache, ask=lambda: self.fail("asked again"), base=self.base)
        self.assertEqual(len(Mock.bodies), 1)

    def test_400_message(self):
        Mock.mode = "400"
        with self.assertRaisesRegex(dl.DownloadError, "did not accept the answers: Choose a role"):
            dl.get_raw(self.cache, answers=ANSWERS, base=self.base)

    def test_429_message(self):
        Mock.mode = "429"
        with self.assertRaisesRegex(dl.DownloadError, "Too many download requests.*about 10 minutes"):
            dl.get_raw(self.cache, answers=ANSWERS, base=self.base)

    def test_503_message(self):
        Mock.mode = "503"
        with self.assertRaisesRegex(dl.DownloadError, "temporarily unavailable"):
            dl.get_raw(self.cache, answers=ANSWERS, base=self.base)

    def test_answers_checked_before_sending(self):
        for change, message in [({"name": ""}, "Enter your name"), ({"email": "nope"}, "valid email"),
                                ({"role": "dean"}, "Choose a role"), ({"agree": False}, "CC BY-NC 4.0"),
                                ({"use": "Other"}, "Say briefly"), ({"name": "x" * 121}, "too long")]:
            with self.assertRaisesRegex(dl.DownloadError, message):
                dl.check_answers({**ANSWERS, **change})
        self.assertEqual(Mock.bodies, [])

    def test_download_url_skips_request(self):
        raw = dl.get_raw(self.cache, download_url=f"{self.base}/v1.0.0/cdd_raw_v1.0.0.zip", ask=lambda: self.fail("asked"))
        self.assertTrue(raw.exists())
        self.assertEqual(Mock.bodies, [])

    def test_raw_path_zip_and_bad_file(self):
        z = Path(self.tmp.name) / "cdd_raw_v1.0.0.zip"
        z.write_bytes(Mock.package)
        self.assertTrue(dl.get_raw(self.cache, raw_path=z).exists())
        bad = Path(self.tmp.name) / "bad.jsonl.gz"
        bad.write_bytes(b"not gzip")
        with self.assertRaises(dl.DownloadError):
            dl.get_raw(self.cache, raw_path=bad)


if __name__ == "__main__":
    unittest.main()
