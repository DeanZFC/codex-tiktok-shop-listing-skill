import importlib.util
import http.client
import os
import re
import ssl
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


def load_module():
    path = Path(__file__).parents[1] / "scripts" / "otuapp_video.py"
    spec = importlib.util.spec_from_file_location("otuapp_video", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


video = load_module()


class RangeHandler(BaseHTTPRequestHandler):
    payload = bytes(range(256)) * 20000

    def do_GET(self):
        header = self.headers.get("Range")
        if header is None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)
            return
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", header)
        if match is None:
            self.send_response(416)
            self.end_headers()
            return
        start, end = map(int, match.groups())
        end = min(end, len(self.payload) - 1)
        chunk = self.payload[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header(
            "Content-Range", f"bytes {start}-{end}/{len(self.payload)}"
        )
        self.end_headers()
        self.wfile.write(chunk)

    def log_message(self, format, *args):
        return


class OtuappVideoTests(unittest.TestCase):
    def test_tls_context_requires_certificate_and_hostname_validation(self):
        self.assertEqual(video.TLS_CONTEXT.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(video.TLS_CONTEXT.check_hostname)

    def test_keychain_token_is_read_without_logging_secret(self):
        completed = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "sk-secret_value\n", "stderr": ""},
        )()
        with patch.object(video.subprocess, "run", return_value=completed) as run:
            token = video._keychain_token("service", "account")
        self.assertEqual(token, "sk-secret_value")
        self.assertNotIn("sk-secret_value", repr(run.call_args))

    def test_submit_uses_two_ordered_reference_parts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt = root / "prompt.txt"
            start = root / "start.png"
            end = root / "end.png"
            prompt.write_text("八秒视频提示词", encoding="utf-8")
            start.write_bytes(b"start-frame")
            end.write_bytes(b"end-frame")
            captured = {}

            def fake_request(method, url, token, **kwargs):
                captured.update(kwargs)
                return {"id": "task-1", "status": "queued", "progress": 0}

            with patch.object(video, "_json_request", side_effect=fake_request):
                result = video.submit(
                    "sk-test",
                    prompt,
                    start,
                    end,
                    video.DEFAULT_MODEL,
                    video.DEFAULT_SIZE,
                )

        body = captured["body"]
        self.assertEqual(body.count(b'name="input_reference"'), 2)
        self.assertLess(body.index(b"start-frame"), body.index(b"end-frame"))
        self.assertIn("八秒视频提示词".encode("utf-8"), body)
        self.assertEqual(result["id"], "task-1")

    def test_public_download_uses_ranges_and_reassembles_exact_bytes(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "video.mp4"
            with patch.dict(
                os.environ,
                {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"},
            ):
                video._download_public_url(
                    f"http://127.0.0.1:{server.server_port}/video.mp4",
                    output,
                )
            self.assertEqual(output.read_bytes(), RangeHandler.payload)

    def test_fetch_range_resumes_after_incomplete_read(self):
        payload = b"0123456789abcdef"
        requested = []

        class Response:
            status = 206

            def __init__(self, start, partial):
                self.start = start
                self.partial = partial
                self.headers = {
                    "Content-Range": f"bytes {start}-{len(payload) - 1}/{len(payload)}"
                }

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                remaining = payload[self.start :]
                if self.partial:
                    raise http.client.IncompleteRead(remaining[:5], len(remaining) - 5)
                return remaining

        def fake_urlopen(request, **kwargs):
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["Range"])
            start = int(match.group(1))
            requested.append(start)
            return Response(start, partial=len(requested) == 1)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "part"
            with patch.object(video.urllib.request, "urlopen", side_effect=fake_urlopen), patch.object(
                video.time, "sleep"
            ):
                video._fetch_range("https://cdn/video.mp4", 0, len(payload) - 1, output)

            self.assertEqual(requested, [0, 5])
            self.assertEqual(output.read_bytes(), payload)

    def test_video_url_search_handles_nested_task_response(self):
        payload = {"data": {"output": {"video_url": "https://cdn/test.mp4"}}}
        self.assertEqual(video._find_video_url(payload), "https://cdn/test.mp4")


if __name__ == "__main__":
    unittest.main()
