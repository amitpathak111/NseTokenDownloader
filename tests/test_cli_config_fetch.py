import gzip
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path

from nse_tokens import __main__ as cli
from nse_tokens.config import ConfigError, load_config
from nse_tokens.fetch_server import FetchError, fetch_to_csv
from tests.test_importer import M1, cm_line, fo_line


def serve(files):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            b = files.get(self.path)
            self.send_response(200 if b is not None else 404)
            self.end_headers()
            self.wfile.write(b if b is not None else b"nope")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def write_cfg(tmp: Path, port=None) -> Path:
    base = f"http://127.0.0.1:{port}" if port else "http://127.0.0.1:9"
    p = tmp / "config.yaml"
    p.write_text(f'''output_csv: {tmp}/shared/tokens.csv
download_dir: {tmp}/dl
cm_url: "{base}/content/cm/NSE_CM_security_{{DDMMYYYY}}.csv.gz"
fo_url: "{base}/content/fo/NSE_FO_contract_{{DDMMYYYY}}.csv.gz"
lookback_days: 3
bhav:
  enabled: false
''')
    return p


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_defaults_and_relative_paths(self):
        (self.tmp / "config.yaml").write_text("output_csv: data/t.csv\n")
        cfg = load_config(self.tmp / "config.yaml")
        self.assertEqual(cfg.output_csv, (self.tmp / "data" / "t.csv").resolve())
        self.assertIn("{DDMMYYYY}", cfg.fo_url)
        self.assertEqual(cfg.indices, {"Nifty 50": 900001, "Nifty Bank": 900002})

    def test_shipped_config_loads(self):
        cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
        self.assertEqual(cfg.lookback_days, 7)

    def test_validation(self):
        for body in ("", "output_csv: x\ncm_url: 'ftp://a/{DDMMYYYY}'\n", "output_csv: x\nfo_url: 'https://a/b.gz'\n",
                     "output_csv: x\nlookback_days: 99\n", "output_csv: x\nindices: {A: x}\n",
                     "output_csv: x\ntimezone: Mars/Base\n", "output_csv: x\nlog_level: LOUD\n"):
            with self.subTest(body=body):
                (self.tmp / "config.yaml").write_text(body)
                with self.assertRaises(ConfigError):
                    load_config(self.tmp / "config.yaml")
        with self.assertRaises(ConfigError):
            load_config(self.tmp / "missing.yaml")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        fo = gzip.compress("\n".join(["NEATFO|1.0"] + [fo_line(i, "FUTSTK", f"S{i}", M1, -1, "XX", lot=100)
                                                       for i in range(1, 30)]).encode())
        cm = gzip.compress("\n".join(["NEATCM|1"] + [cm_line(1000 + i, f"S{i}", "EQ") for i in range(1, 30)]).encode())
        self.srv = serve({"/content/fo/NSE_FO_contract_06112026.csv.gz": fo,
                          "/content/cm/NSE_CM_security_06112026.csv.gz": cm})
        self.addCleanup(self.srv.shutdown)
        self.cfg = write_cfg(self.tmp, self.srv.server_port)

    def run_cli(self, *argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = cli.main(["--config", str(self.cfg), *argv])
        return code, buf.getvalue()

    def test_update_downloads_imports_and_info_reports(self):
        code, out = self.run_cli("update", "--date", "2026-11-08")       # Sunday -> falls back to Fri 6 Nov
        self.assertEqual(code, 0, out)
        self.assertTrue((self.tmp / "shared" / "tokens.csv").exists())
        self.assertEqual(json.loads((self.tmp / "shared" / "tokens.csv.meta.json").read_text())["rows"], 60)
        code, out = self.run_cli("info")
        self.assertEqual(code, 0)
        self.assertIn("rows     : 60", out)

    def test_download_only_and_import_dry_run(self):
        self.assertEqual(self.run_cli("download", "--date", "2026-11-06")[0], 0)
        self.assertFalse((self.tmp / "shared" / "tokens.csv").exists())
        fo = self.tmp / "dl" / "NSE_FO_contract_06112026.csv.gz"
        cm = self.tmp / "dl" / "NSE_CM_security_06112026.csv.gz"
        code, out = self.run_cli("import", "--fo", str(fo), "--cm", str(cm), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertFalse((self.tmp / "shared" / "tokens.csv").exists())

    def test_custom_index_and_bad_index_spec(self):
        self.assertEqual(self.run_cli("update", "--date", "2026-11-06", "--no-default-indices",
                                      "--index", "Nifty 50=12345")[0], 0)
        text = (self.tmp / "shared" / "tokens.csv").read_text()
        self.assertIn("12345,NIFTY 50,Nifty 50", text)
        self.assertNotIn("900002", text)
        self.assertEqual(self.run_cli("update", "--date", "2026-11-06", "--index", "oops")[0], 2)

    def test_download_failure_exit_code_and_info_before_first_run(self):
        self.cfg = write_cfg(self.tmp, None)
        self.assertEqual(self.run_cli("update", "--date", "2026-11-06")[0], 1)
        self.assertEqual(self.run_cli("info")[0], 1)


class FetchServerTests(unittest.TestCase):
    def test_json_to_csv_and_bad_inputs_keep_existing_file(self):
        rows = [{"ScriptCode": 1, "Symbol": "A", "ScriptName": "A", "Series": "EQ", "LotSize": 1, "Close": 10.5,
                 "Volume": 5, "OI": 0, "Nifty": "NA", "Future": "NA", "Sector": None, "Price_Jump": 0.05,
                 "Strike_Price": None, "Option_Type": None, "Expiry": None, "LB": "1"}]
        srv = serve({"/ok": json.dumps(rows).encode(), "/empty": b"[]", "/junk": b"not json"})
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"
        out = Path(tempfile.mkdtemp()) / "t.csv"
        self.assertEqual(fetch_to_csv(base + "/ok", out), 1)
        self.assertNotIn("LB", out.read_text())
        good = out.read_text()
        for path in ("/empty", "/junk", "/missing"):
            with self.subTest(path=path), self.assertRaises(FetchError):
                fetch_to_csv(base + path, out)
        with self.assertRaises(FetchError):
            fetch_to_csv("ftp://x/y", out)
        self.assertEqual(out.read_text(), good)


if __name__ == "__main__":
    unittest.main()
