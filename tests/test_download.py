import gzip
import tempfile
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from nse_tokens import download as nd

import os
PAYLOAD = gzip.compress(b"NEATFO|1.0\n" + b"\n".join(os.urandom(12).hex().encode() for _ in range(200)))


def serve(files: dict[str, bytes], status_for_missing=404):
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, self.headers.get("User-Agent"), self.headers.get("Referer")))
            body = files.get(self.path)
            self.send_response(200 if body is not None else status_for_missing)
            self.end_headers()
            self.wfile.write(body or b"<html>Access Denied</html>")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sleep = mock.patch("nse_tokens.download.time.sleep").start()
        self.addCleanup(mock.patch.stopall)

    def tpl(self, srv):
        return f"http://127.0.0.1:{srv.server_port}/content/fo/NSE_FO_contract_{{DDMMYYYY}}.csv.gz"

    def test_downloads_and_sends_browser_headers(self):
        srv, hits = serve({"/content/fo/NSE_FO_contract_06112026.csv.gz": PAYLOAD})
        self.addCleanup(srv.shutdown)
        p = nd.download_one(self.tpl(srv), date(2026, 11, 6), self.tmp, 5, 3)
        self.assertEqual(p.name, "NSE_FO_contract_06112026.csv.gz")
        self.assertEqual(p.read_bytes(), PAYLOAD)
        self.assertIn("Mozilla", hits[0][1])
        self.assertEqual(hits[0][2], "https://www.nseindia.com/")
        self.assertFalse(list(self.tmp.glob("*.part")))

    def test_falls_back_over_a_closed_weekend_and_warns(self):
        # Monday 9-Nov-2026 has no file; Sat/Sun have none; Friday 6-Nov does
        srv, hits = serve({"/content/fo/NSE_FO_contract_06112026.csv.gz": PAYLOAD})
        self.addCleanup(srv.shutdown)
        with self.assertLogs("nse_tokens.download", "WARNING") as cm:
            p = nd.download_one(self.tpl(srv), date(2026, 11, 9), self.tmp, 5, 5)
        self.assertEqual(p.name, "NSE_FO_contract_06112026.csv.gz")
        self.assertEqual([h[0][-15:-7] for h in hits], ["09112026", "08112026", "07112026", "06112026"])
        self.assertTrue(any("NOT 09-Nov-2026" in m and "06-Nov-2026" in m for m in cm.output))

    def test_weekend_special_session_file_is_used(self):
        # Budget-day style: Saturday 1-Feb-2026 has a file even though it is a weekend
        srv, hits = serve({"/content/fo/NSE_FO_contract_01022026.csv.gz": PAYLOAD})
        self.addCleanup(srv.shutdown)
        p = nd.download_one(self.tpl(srv), date(2026, 2, 1), self.tmp, 5, 3)
        self.assertEqual(p.name, "NSE_FO_contract_01022026.csv.gz")
        self.assertEqual(len(hits), 1)

    def test_no_warning_when_todays_file_is_used(self):
        srv, _ = serve({"/content/fo/NSE_FO_contract_06112026.csv.gz": PAYLOAD})
        self.addCleanup(srv.shutdown)
        with self.assertNoLogs("nse_tokens.download", "WARNING"):
            nd.download_one(self.tpl(srv), date(2026, 11, 6), self.tmp, 5, 3)

    def test_403_failure_mentions_possible_blocking(self):
        srv, _ = serve({}, status_for_missing=403)
        self.addCleanup(srv.shutdown)
        with self.assertRaisesRegex(nd.NotAvailable, "blocking this machine"):
            nd.download_one(self.tpl(srv), date(2026, 11, 6), self.tmp, 5, 1)

    def test_404_failure_does_not_claim_blocking(self):
        srv, _ = serve({})
        self.addCleanup(srv.shutdown)
        with self.assertRaises(nd.NotAvailable) as cm:
            nd.download_one(self.tpl(srv), date(2026, 11, 6), self.tmp, 5, 1)
        self.assertNotIn("blocking", str(cm.exception))

    def test_not_available_raises_with_helpful_message(self):
        srv, _ = serve({})
        self.addCleanup(srv.shutdown)
        with self.assertRaisesRegex(nd.NotAvailable, "published after end of day"):
            nd.download_one(self.tpl(srv), date(2026, 11, 6), self.tmp, 5, 2)

    def test_html_block_page_is_rejected_and_not_saved(self):
        srv, _ = serve({"/content/fo/NSE_FO_contract_06112026.csv.gz": b"<html>" + b"blocked " * 100 + b"</html>"})
        self.addCleanup(srv.shutdown)
        with self.assertRaisesRegex(nd.DownloadError, "did not return a gzip"):
            nd.download_one(self.tpl(srv), date(2026, 11, 6), self.tmp, 5, 0)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_corrupt_gzip_rejected(self):
        bad = PAYLOAD[:40] + b"garbage" * 40
        srv, _ = serve({"/content/fo/NSE_FO_contract_06112026.csv.gz": bad})
        self.addCleanup(srv.shutdown)
        with self.assertRaises(nd.DownloadError):
            nd.download_one(self.tpl(srv), date(2026, 11, 6), self.tmp, 5, 0)

    def test_truncated_download_rejected(self):
        srv, _ = serve({"/content/fo/NSE_FO_contract_06112026.csv.gz": PAYLOAD[:-30]})
        self.addCleanup(srv.shutdown)
        with self.assertRaisesRegex(nd.DownloadError, "corrupt or truncated"):
            nd.download_one(self.tpl(srv), date(2026, 11, 6), self.tmp, 5, 0)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_existing_file_is_reused(self):
        (self.tmp / "NSE_FO_contract_06112026.csv.gz").write_bytes(PAYLOAD)
        srv, hits = serve({})
        self.addCleanup(srv.shutdown)
        nd.download_one(self.tpl(srv), date(2026, 11, 6), self.tmp, 5, 0)
        self.assertEqual(hits, [])

    def test_unreachable_server_raises_download_error(self):
        with self.assertRaises(nd.DownloadError):
            nd.download_one("http://127.0.0.1:9/x_{DDMMYYYY}.gz", date(2026, 11, 6), self.tmp, 1, 0)


if __name__ == "__main__":
    unittest.main()
