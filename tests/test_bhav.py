import io
import tempfile
import unittest
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path

from nse_tokens import bhav as nb
from nse_tokens import download as nd
from nse_tokens import importer as ni
from nse_tokens.config import load_config
from tests.test_download import serve
from tests.test_importer import M1, cm_line, fo_line

UDIFF_HDR = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
             "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
             "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks")


def udiff_row(token, sym, close, vol, oi="", settle=None):
    f = ["2026-11-06", "2026-11-06", "FO", "NSE", "STF", token, "", sym, "", "", "", "", "", sym, 1, 1, 1, close,
         close, 1, "", settle if settle is not None else close, oi, 0, vol, 0, 0, "F1", 1, ""]
    return ",".join(str(x) for x in f)


def make_zip(path: Path, rows):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("BhavCopy.csv", "\n".join([UDIFF_HDR, *rows]))
    return path


class BhavTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_read_bhav_zip(self):
        p = make_zip(self.tmp / "b.csv.zip", [udiff_row(101, "RELIANCE", 2890.5, 1000, 777, settle=2891.0)])
        self.assertEqual(nb.read_bhav(p), {101: {"close": 2890.5, "volume": 1000, "oi": 777}})
        self.assertEqual(nb.read_bhav(p, "settlement")[101]["close"], 2891.0)

    def test_wrong_layout_fails_loudly(self):
        p = self.tmp / "x.csv"
        p.write_text("a,b,c\n1,2,3\n")
        with self.assertRaisesRegex(ni.NseImportError, "missing column"):
            nb.read_bhav(p)

    def test_mostly_bad_rows_rejected(self):
        p = make_zip(self.tmp / "b.zip", [udiff_row("abc", "X", "n/a", 1)] * 5)
        with self.assertRaises(ni.NseImportError):
            nb.read_bhav(p)

    def test_index_close(self):
        p = self.tmp / "ind.csv"
        p.write_text("Index Name,Index Date,Closing Index Value\nNifty 50,06-11-2026,\"24,500.25\"\nNifty Bank,06-11-2026,52000\n")
        self.assertEqual(nb.read_index_close(p), {"nifty 50": 24500.25, "nifty bank": 52000.0})

    def test_build_applies_bhav(self):
        fo = self.tmp / "contract.txt"
        fo.write_bytes("\n".join(["NEATFO|1.0", fo_line(101, "FUTSTK", "RELIANCE", M1, -1, "XX", lot=250, base=1),
                                  fo_line(102, "FUTSTK", "TCS", M1, -1, "XX", lot=175, base=350000)]).encode())
        cm = self.tmp / "security.txt"
        cm.write_bytes("\n".join(["NEATCM|01.00.00|1", cm_line(1001, "RELIANCE", "EQ"),
                                  cm_line(1002, "NOBHAV", "EQ")]).encode())
        bhav = {"fo": {101: {"close": 2890.5, "volume": 5000, "oi": 12500}},
                "cm": {1001: {"close": 2888.0, "volume": 900000, "oi": 0}},
                "idx": {"nifty 50": 24500.25}}
        rows, stats = ni.build(fo, cm, None, {"Nifty 50": 900001, "Nifty Bank": 900002}, bhav=bhav)
        by = {r["ScriptCode"]: r for r in rows}
        self.assertEqual((by[101]["Close"], by[101]["Volume"], by[101]["OI"]), (2890.5, 5000, 12500))
        self.assertEqual(by[102]["Close"], 3500.0)                       # no bhav row: master base price kept
        self.assertEqual((by[1001]["Close"], by[1001]["Volume"]), (2888.0, 900000))
        self.assertEqual(by[1002]["Close"], 0)
        self.assertEqual(by[900001]["Close"], 24500.25)
        self.assertEqual(by[900002]["Close"], 0)
        self.assertEqual((stats["bhav_matched_fo"], stats["bhav_unmatched_fo"]), (1, 1))
        self.assertEqual((stats["bhav_matched_cm"], stats["bhav_unmatched_cm"]), (1, 1))
        self.assertEqual((stats["bhav_matched_index"], stats["bhav_unmatched_index"]), (1, 1))

    def test_no_bhav_leaves_rows_alone(self):
        rows = [{"MarketCode": 3, "Series": "EQ", "ScriptCode": 1, "ScriptName": "A", "Close": 0, "Volume": 0, "OI": 0}]
        nb.apply(rows, None, None, None, Counter())
        self.assertEqual(rows[0]["Close"], 0)


class BhavDownloadTests(unittest.TestCase):
    def test_zip_download_with_yyyymmdd_and_weekend(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("b.csv", UDIFF_HDR + "\n" + "\n".join(udiff_row(i, "S", 1, 1) for i in range(5)))
        srv, hits = serve({"/BhavCopy_20261108_F.csv.zip": buf.getvalue()})
        self.addCleanup(srv.shutdown)
        tmp = Path(tempfile.mkdtemp())
        p = nd.download_one(f"http://127.0.0.1:{srv.server_port}/BhavCopy_{{YYYYMMDD}}_F.csv.zip",
                            date(2026, 11, 9), tmp, 5, 3)
        self.assertEqual(p.name, "BhavCopy_20261108_F.csv.zip")        # Sunday session file found
        self.assertEqual(len(hits), 2)

    def test_html_instead_of_zip_rejected(self):
        srv, _ = serve({"/b_20261106.csv.zip": b"<html>" + b"x" * 400})
        self.addCleanup(srv.shutdown)
        with self.assertRaises(nd.DownloadError):
            nd.download_one(f"http://127.0.0.1:{srv.server_port}/b_{{YYYYMMDD}}.csv.zip", date(2026, 11, 6),
                            Path(tempfile.mkdtemp()), 5, 0)

    def test_shipped_config_has_bhav(self):
        cfg = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
        self.assertTrue(cfg.bhav_enabled)
        self.assertIn("{YYYYMMDD}", cfg.bhav_fo_url)
        self.assertEqual(cfg.bhav_fo_close, "close")


class BhavCliTests(unittest.TestCase):
    def test_update_end_to_end_with_bhav_and_missing_index_file(self):
        import csv
        import gzip
        from contextlib import redirect_stdout
        from nse_tokens import __main__ as cli

        def zbytes(rows):
            b = io.BytesIO()
            with zipfile.ZipFile(b, "w") as z:
                z.writestr("b.csv", "\n".join([UDIFF_HDR, *rows]))
            return b.getvalue()

        fo = gzip.compress("\n".join(["NEATFO|1.0"] + [fo_line(i, "FUTSTK", f"S{i}", M1, -1, "XX", lot=100)
                                                       for i in range(1, 30)]).encode())
        cm = gzip.compress("\n".join(["NEATCM|1"] + [cm_line(1000 + i, f"S{i}", "EQ") for i in range(1, 30)]).encode())
        srv, _ = serve({"/fo/NSE_FO_contract_06112026.csv.gz": fo, "/cm/NSE_CM_security_06112026.csv.gz": cm,
                        "/fo/bhavfo_20261106.zip": zbytes([udiff_row(i, f"S{i}", 100 + i, 10 * i, 5 * i) for i in range(1, 30)]),
                        "/cm/bhavcm_20261106.zip": zbytes([udiff_row(1000 + i, f"S{i}", 50 + i, 7 * i) for i in range(1, 30)])})
        self.addCleanup(srv.shutdown)
        tmp, b = Path(tempfile.mkdtemp()), f"http://127.0.0.1:{srv.server_port}"
        (tmp / "config.yaml").write_text(f"""output_csv: {tmp}/t.csv
download_dir: {tmp}/dl
cm_url: "{b}/cm/NSE_CM_security_{{DDMMYYYY}}.csv.gz"
fo_url: "{b}/fo/NSE_FO_contract_{{DDMMYYYY}}.csv.gz"
lookback_days: 1
bhav:
  cm_url: "{b}/cm/bhavcm_{{YYYYMMDD}}.zip"
  fo_url: "{b}/fo/bhavfo_{{YYYYMMDD}}.zip"
  index_url: "{b}/idx/ind_{{DDMMYYYY}}.csv"
""")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--config", str(tmp / "config.yaml"), "update", "--date", "2026-11-06"]), 0)
        rows = {int(r["ScriptCode"]): r for r in csv.DictReader(open(tmp / "t.csv"))}
        self.assertEqual((float(rows[3]["Close"]), rows[3]["Volume"], rows[3]["OI"]), (103.0, "30", "15"))
        self.assertEqual((float(rows[1005]["Close"]), rows[1005]["Volume"]), (55.0, "35"))
        self.assertEqual(float(rows[900001]["Close"]), 0)          # index file missing -> warning, rest still applied


if __name__ == "__main__":
    unittest.main()
