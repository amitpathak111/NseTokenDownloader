import gzip
import tempfile
import unittest
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import csv
import json

from nse_tokens import importer as ni


def secs(d: date) -> int:
    return int((datetime(d.year, d.month, d.day) - ni.EPOCH).total_seconds())


def fo_line(token, inst, sym, expiry, strike, opt, lot=50, tick=5, base=10000, delete="N", permitted=1):
    f = [""] * 60
    f[0], f[1], f[2], f[3], f[4], f[6], f[7], f[8] = token, 1, inst, sym, "XX", secs(expiry), strike, opt
    f[12], f[31], f[32], f[-2], f[-1] = permitted, lot, tick, base, delete
    return "|".join(str(x) for x in f)


def cm_line(token, sym, series, lot=1, tick=5, permitted=1, delete="N"):
    f = [""] * 45
    f[0], f[1], f[2], f[5], f[19], f[20], f[-3] = token, sym, series, permitted, lot, tick, delete
    f[-2], f[-1] = 10, "INE000A01010"            # Face Value, ISIN - real rows always end with these
    return "|".join(str(x) for x in f)


E1, E2, E3, E4, E5 = (date(2026, 11, 3), date(2026, 11, 10), date(2026, 11, 17), date(2026, 11, 24), date(2026, 12, 1))
M1, M2, M3 = date(2026, 11, 24), date(2026, 12, 29), date(2027, 1, 26)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def write(self, name, lines, gz=False, header="NEATFO|1.0.0"):
        p = self.tmp / name
        body = "\n".join([header] + lines).encode("latin-1")
        p.write_bytes(gzip.compress(body) if gz else body)
        return p

    def fo_file(self, gz=False):
        lines = [
            fo_line(101, "FUTSTK", "RELIANCE", M1, -1, "XX", lot=250),
            fo_line(102, "OPTSTK", "RELIANCE", M1, 290000, "CE", lot=250, tick=5),
            fo_line(103, "FUTIDX", "NIFTY", M1, -1, "XX", lot=75),
            fo_line(104, "FUTIDX", "NIFTY", M2, -1, "XX", lot=75),
            fo_line(105, "FUTIDX", "NIFTY", M3, -1, "XX", lot=75),          # third month: dropped
            *[fo_line(110 + i, "OPTIDX", "NIFTY", e, 2450000, "PE", lot=75) for i, e in enumerate((E1, E2, E3, E4, E5))],
            fo_line(120, "OPTIDX", "FINNIFTY", M1, 2350000, "CE", lot=65),
            fo_line(121, "OPTIDX", "MIDCPNIFTY", M1, 1300000, "CE", lot=120),
            fo_line(130, "FUTSTK", "DEADCO", M1, -1, "XX", delete="Y"),
            fo_line(131, "FUTSTK", "BSEONLY$", M1, -1, "XX"),
            fo_line(132, "FUTSTK", "TESTNSETEST", M1, -1, "XX"),
            fo_line(133, "FUTSTK", "NOTPERM", M1, -1, "XX", permitted=0),
            fo_line(140, "FUTINT", "USDINR", M1, -1, "XX"),
        ]
        return self.write("contract.txt" + (".gz" if gz else ""), lines, gz)

    def cm_file(self):
        lines = [cm_line(1001, "RELIANCE", "EQ"), cm_line(1002, "SMALLCO", "EQ"), cm_line(1003, "SMEFIRM", "SM"),
                 cm_line(1004, "ODDLOT", "BE"), cm_line(1005, "GONE", "EQ", delete="Y"),
                 cm_line(1006, "ZZNSETEST", "EQ")]
        return self.write("security.txt", lines, header="NEATCM|01.00.00|1")

    def test_fo_mapping(self):
        rows, stats = ni.build(self.fo_file(gz=True), None, None, {}, dry_run=True)
        by = {r["ScriptCode"]: r for r in rows}
        self.assertEqual({c for c in by}, {101, 102, 103, 104, 110, 111, 112, 113, 114, 120, 121})
        self.assertEqual(by[101]["MarketCode"], 0)
        self.assertEqual(by[102]["MarketCode"], 2)
        self.assertEqual((by[103]["MarketCode"], by[104]["MarketCode"]), (5, 6))
        self.assertEqual([by[110 + i]["MarketCode"] for i in range(5)], [7, 8, 9, 14, 1])
        self.assertEqual((by[120]["MarketCode"], by[121]["MarketCode"]), (11, 12))
        o = by[102]
        self.assertEqual((o["Strike_Price"], o["Option_Type"], o["Expiry"], o["LotSize"], o["Close"], o["Price_Jump"]),
                         (2900.0, "CE", "2026-11-24", 250, 100.0, 0.05))
        self.assertEqual(by[102]["ScriptName"], "RELIANCE24NOV262900CE")
        self.assertEqual(by[103]["ScriptName"], "NIFTY24NOV26FUT")
        self.assertEqual(by[103]["Strike_Price"], "")
        self.assertEqual(stats["fo_far_index_future_skipped"], 1)
        self.assertEqual(stats["fo_skipped_other_instrument"], 1)
        self.assertEqual(stats["fo_inactive_skipped"], 4)

    def test_cash_mapping_uses_fo_symbols_for_code_4(self):
        rows, _ = ni.build(self.fo_file(), self.cm_file(), None, ni.DEFAULT_INDICES, dry_run=True)
        by = {r["ScriptCode"]: r for r in rows}
        self.assertEqual((by[1001]["MarketCode"], by[1001]["Future"]), (4, "F"))      # has stock F&O
        self.assertEqual((by[1002]["MarketCode"], by[1002]["Future"]), (3, "NA"))
        self.assertEqual(by[1003]["MarketCode"], 13)
        for gone in (1004, 1005, 1006):
            self.assertNotIn(gone, by)
        self.assertEqual({by[900001]["ScriptName"], by[900002]["ScriptName"]}, {"Nifty 50", "Nifty Bank"})
        self.assertEqual(by[900001]["MarketCode"], 10)

    def test_output_csv_follows_the_shared_contract_and_writes_metadata(self):
        out = self.tmp / "tokens.csv"
        rows, _ = ni.build(self.fo_file(), self.cm_file(), out, ni.DEFAULT_INDICES)
        with open(out, newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            self.assertEqual(tuple(rd.fieldnames), ni.OUT_COLUMNS)
            data = list(rd)
        self.assertEqual(len(data), len(rows))
        self.assertEqual(len({r["ScriptCode"] for r in data}), len(data))               # tokens unique
        self.assertEqual(Counter(r["MarketCode"] for r in data),
                         Counter({"7": 1, "8": 1, "9": 1, "14": 1, "1": 1, "0": 1, "2": 1, "5": 1, "6": 1,
                                  "11": 1, "12": 1, "4": 1, "3": 1, "13": 1, "10": 2}))
        meta = json.loads((self.tmp / "tokens.csv.meta.json").read_text())
        self.assertEqual((meta["rows"], meta["sources"]["fo"], meta["sources"]["cm"]),
                         (len(rows), "contract.txt", "security.txt"))
        self.assertEqual(meta["columns"], list(ni.OUT_COLUMNS))
        self.assertTrue(meta["generated_at_utc"].endswith("+00:00"))

    def test_named_header_layout_with_date_expiry(self):
        p = self.write("c.csv", ["12345,FUTSTK,TCS,27NOV2026,-1,XX,175,5", "12346,OPTSTK,TCS,27NOV2026,400000,PE,175,5"],
                       header="Token,InstrumentType,Symbol,ExpiryDate,StrikePrice,OptionType,BoardLotQuantity,TickSize")
        rows, _ = ni.build(p, None, None, {}, dry_run=True)
        self.assertEqual([(r["ScriptCode"], r["MarketCode"], r["Expiry"]) for r in rows],
                         [(12345, 0, "2026-11-27"), (12346, 2, "2026-11-27")])
        self.assertEqual(rows[1]["Strike_Price"], 4000.0)

    def test_wrong_layout_aborts_without_writing(self):
        p = self.write("junk.txt", ["abc|def|ghi"] * 10)
        out = self.tmp / "tokens.csv"
        with self.assertRaisesRegex(ni.NseImportError, "do not match the expected NSE layout"):
            ni.build(p, None, out, {})
        self.assertFalse(out.exists())

    def test_named_header_missing_required_column(self):
        p = self.write("h.csv", ["1,FUTSTK,X"], header="Token,InstrumentType,Symbol")
        with self.assertRaisesRegex(ni.NseImportError, "no column for"):
            ni.build(p, None, None, {}, dry_run=True)

    def test_existing_csv_survives_failed_import_and_args_are_checked(self):
        out = self.tmp / "tokens.csv"
        out.write_text("keep")
        with self.assertRaises(ni.NseImportError):
            ni.build(self.write("junk.txt", ["x|y"] * 5), None, out, {})
        self.assertEqual(out.read_text(), "keep")
        with self.assertRaises(ni.NseImportError):
            ni.build(None, None, out, {})
        with self.assertRaises(ni.NseImportError):
            ni.build(self.tmp / "missing.gz", None, out, {})



REAL_FO_ROWS = [   # copied verbatim from the user's real NSE_FO_contract file (first 3 data rows)
    "35000,26009,OPTIDX,BANKNIFTY,1483021800,6940000,CE,1,30,30,5,1475280000,0,1483021800,,XX,,,BANKNIFTY26DEC69400CE,C,3810,N,1482969600,1483021800,1.000000E+12,5,8105,1,0,0,0,2,1,2,0,2,0,3,0,0,9.010000E+02,0,1475280000,0,0,0,0,0,0,0,0,1478450880,15,1,            ,0,0,0,0,,E,N,N,N,N,,,,,,,,,,,,,,,,,,,,,",
    "35001,26009,OPTIDX,BANKNIFTY,1483021800,6940000,PE,1,30,30,5,1475280000,0,1483021800,,XX,,,BANKNIFTY26DEC69400PE,C,971730,N,1482969600,1483021800,1.000000E+12,791850,1151610,1,0,0,0,2,1,2,0,2,0,3,0,0,9.010000E+02,0,1475280000,0,0,0,0,0,0,0,0,1478450880,15,1,            ,0,0,0,0,,E,N,N,N,N,,,,,,,,,,,",
    "35002,26009,OPTIDX,BANKNIFTY,1483021800,6960000,CE,1,30,30,5,1475280000,0,1483021800,,XX,,,BANKNIFTY26DEC69600CE,C,3440,N,1482969600,1483021800,1.000000E+12,5,7365,1,0,0,0,2,1,2,0,2,0,3,0,0,9.010000E+02,0,1475280000,0,0,0,0,0,0,0,0,1478450880,15,1,            ,0,0,0,0,,E,N,N,N,N,,,,,,,,,,,,,,,,,,,,,",
]


class RealCsvLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_real_comma_separated_rows_are_parsed(self):
        fut = REAL_FO_ROWS[0].replace("OPTIDX", "FUTSTK").replace("BANKNIFTY", "TCS").replace("6940000,CE", "-1,XX").replace("TCS26DEC69400CE", "TCS26DECFUT").replace("35000", "35010")
        p = self.tmp / "NSE_FO_contract_06112026.csv.gz"
        p.write_bytes(gzip.compress("\n".join(REAL_FO_ROWS + [fut]).encode()))
        rows, stats = ni.build(p, None, None, {}, dry_run=True)
        by = {r["ScriptCode"]: r for r in rows}
        self.assertEqual(set(by), {35000, 35001, 35002, 35010})
        r = by[35000]
        self.assertEqual((r["Symbol"], r["Series"], r["Expiry"], r["Strike_Price"], r["Option_Type"], r["LotSize"]),
                         ("BANKNIFTY", "OPTIDX", "2026-12-29", 69400.0, "CE", 30))
        self.assertEqual((r["Price_Jump"], r["Close"], r["ScriptName"]), (0.05, 38.1, "BANKNIFTY29DEC2669400CE"))
        self.assertEqual(by[35001]["Close"], 9717.3)
        self.assertEqual(by[35010]["MarketCode"], 0)
        self.assertEqual(by[35000]["MarketCode"], 7)                       # only expiry for BANKNIFTY here -> nearest
        self.assertEqual(stats["fo_name_mismatch"], 0)

    def test_comma_cash_file_uses_leading_columns(self):
        cm = self.tmp / "NSE_CM_security_06112026.csv.gz"
        cm.write_bytes(gzip.compress("\n".join(["2885,RELIANCE,EQ,0,1,2", "1594,SMALLCO,EQ,0,1,2", "9999,SME1,SM,0,1,2",
                                               "7,BONDX,N1,0,1,2", "8,GHOSTNSETEST,EQ,0,1,2"]).encode()))
        fo = self.tmp / "fo.gz"
        fo.write_bytes(gzip.compress("\n".join(REAL_FO_ROWS).replace("BANKNIFTY", "RELIANCE").replace("OPTIDX", "OPTSTK").encode()))
        rows, _ = ni.build(fo, cm, None, {}, dry_run=True)
        cash = {r["ScriptCode"]: r for r in rows if r["Series"] in ("EQ", "SM")}
        self.assertEqual((cash[2885]["MarketCode"], cash[1594]["MarketCode"], cash[9999]["MarketCode"]), (4, 3, 13))
        self.assertEqual(set(cash), {2885, 1594, 9999})

    def test_unvalidated_delete_column_is_ignored(self):
        bad = [r.split(",") for r in REAL_FO_ROWS]
        for b in bad:
            b[21] = "weird"
        p = self.tmp / "fo.gz"
        p.write_bytes(gzip.compress("\n".join(",".join(b) for b in bad).encode()))
        rows, _ = ni.build(p, None, None, {}, dry_run=True)
        self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()
