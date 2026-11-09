"""Build the token CSV from NSE's own master files.

Inputs (download them yourself - see README):
  --fo  NSE_FO_contract_ddmmyyyy.csv.gz   (or the pipe-delimited contract.gz / contract.txt)
  --cm  NSE_CM_security_ddmmyyyy.csv.gz   (or security.gz / security.txt)

Two layouts are understood:
  * files whose first line holds column NAMES (matched by name, order does not matter);
  * the pipe-delimited ``contract.txt`` / ``security.txt`` layout from NSE's "Masters Data"
    specification (matched by field POSITION as published there).
Every row is sanity-checked; if the file does not look like what we expect the import aborts and
shows the offending row rather than writing a wrong token list.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CSV_COLUMNS = ("ScriptCode", "Symbol", "ScriptName", "Series", "LotSize", "Close", "Volume", "OI", "Nifty",
               "Future", "Sector", "Price_Jump", "Strike_Price", "Option_Type", "Expiry")

EPOCH = datetime(1980, 1, 1)            # NSE time fields: seconds since 1980-01-01
FO_TYPES = {"FUTSTK", "FUTIDX", "OPTSTK", "OPTIDX"}
OUT_COLUMNS = CSV_COLUMNS + ("MarketCode",)
DEFAULT_INDICES = {"Nifty 50": 900001, "Nifty Bank": 900002}

# 0-based positions in NSE's pipe-delimited masters (NSE Masters Data spec v1.10, 3.1.1 and 3.2.1)
FO_POS = {"token": 0, "instrument": 2, "symbol": 3, "series": 4, "expiry": 6, "strike": 7, "option": 8,
          "permitted": 12, "board_lot": 31, "tick": 32, "base_price": -2, "delete": -1}
# Layout of the comma-separated, header-less NSE_FO_contract_*.csv.gz as published on nseindia.com
# (verified against real rows: expiry/strike/name agree, e.g. "...,BANKNIFTY,1483021800,6940000,CE,...,BANKNIFTY26DEC69400CE").
# Positions 7+ of that file are only partly known; lot size = board lot (col 9), tick (col 10), base price (col 20).
FO_CSV_POS = {"token": 0, "instrument": 2, "symbol": 3, "expiry": 4, "strike": 5, "option": 6,
              "board_lot": 9, "tick": 10, "name": 18, "base_price": 20, "delete": 21}
# Cash security CSV: only the leading columns (Token, Symbol, Series) are relied on; lot size defaults to 1.
CM_CSV_POS = {"token": 0, "symbol": 1, "series": 2}
CM_POS = {"token": 0, "symbol": 1, "series": 2, "permitted": 5, "board_lot": 19, "tick": 20, "delete": -3}

ALIASES = {
    "token": ("token", "tokenno", "tokennumber"), "instrument": ("instrumenttype", "instrument", "insttype"),
    "symbol": ("symbol",), "series": ("series",), "expiry": ("expirydate", "expiry"),
    "strike": ("strikeprice", "strike"), "option": ("optiontype", "opttype"),
    "permitted": ("permittedtotrade",), "board_lot": ("boardlotquantity", "boardlot", "lotsize", "marketlot"),
    "tick": ("ticksize",), "base_price": ("baseprice",), "delete": ("deleteflag",),
}
MAX_BAD_FRACTION = 0.02


class NseImportError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- reading
def _read_text(path: Path) -> str:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise NseImportError(f"cannot read {path}: {exc}") from exc
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("latin-1")


def _split(line: str, delim: str) -> list[str]:
    cells = line.rstrip("\r\n").split(delim)
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return [c.strip() for c in cells]


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def read_master(path: Path, positions: dict[str, int], required: tuple[str, ...],
                csv_positions: dict[str, int] | None = None) -> list[dict[str, str]]:
    lines = [ln for ln in _read_text(path).splitlines() if ln.strip()]
    if not lines:
        raise NseImportError(f"{path}: file is empty")
    delim = "|" if lines[0].count("|") >= lines[0].count(",") else ","
    first = _split(lines[0], delim)
    named = {_norm(c) for c in first}
    colmap: dict[str, int] | None = None
    start = 0
    if not first[0].lstrip("-").isdigit():                    # a header line of some kind
        start = 1
        for key, aliases in ALIASES.items():
            for a in aliases:
                if a in named:
                    colmap = colmap or {}
                    colmap[key] = [_norm(c) for c in first].index(a)
                    break
        if colmap is not None:
            missing = [k for k in required if k not in colmap]
            if missing:
                raise NseImportError(f"{path}: header has no column for {missing}; header was: {first[:15]}")
    layout = colmap or (csv_positions if (csv_positions and delim == ",") else positions)
    rows: list[dict[str, str]] = []
    for ln in lines[start:]:
        cells = _split(ln, delim)
        row: dict[str, str] = {}
        for key, idx in layout.items():
            try:
                row[key] = cells[idx]
            except IndexError:
                pass
        row["_raw"] = ln[:300]
        rows.append(row)
    return rows


# ----------------------------------------------------------------------------- value parsing
def _int(v: str) -> int:
    return int(float(v))


def _opt_int(v: str | None) -> int:
    try:
        return _int(v) if v not in (None, "") else 0
    except ValueError:
        return 0


def _expiry(v: str) -> date:
    v = v.strip()
    if v.lstrip("-").isdigit():
        n = int(v)
        if not 5e8 <= n <= 2.5e9:
            raise ValueError(f"expiry {n} is not seconds-since-1980")
        return (EPOCH + timedelta(seconds=n)).date()
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(v.upper(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unrecognised expiry {v!r}")


def _active(row: dict[str, str]) -> bool:
    if row.get("delete", "N").upper() == "Y":
        return False
    if row.get("permitted", "1") not in ("1", ""):             # 0 = not tradable, 2 = BSE-listed
        return False
    sym = row.get("symbol", "")
    return not (sym.endswith("NSETEST") or "$" in sym)


def _drop_unvalidated_delete_flag(rows: list[dict[str, str]]) -> None:
    """Use the delete-flag column only if every value is Y/N; otherwise the position is wrong, so ignore it."""
    if rows and not {r.get("delete", "N").upper() for r in rows} <= {"Y", "N"}:
        for r in rows:
            r.pop("delete", None)


def _check_quality(name: str, good: int, bad: list[str]) -> None:
    total = good + len(bad)
    if total == 0 or good == 0 or len(bad) / total > MAX_BAD_FRACTION:
        sample = "\n  ".join(bad[:3]) or "(no usable rows)"
        raise NseImportError(
            f"{name}: {len(bad)} of {total} rows do not match the expected NSE layout, so the column "
            f"positions/names are probably different in this file.\nFirst bad rows:\n  {sample}\n"
            "Nothing was written. Send me the first 3 lines of the file and I will adjust the mapping.")


# ----------------------------------------------------------------------------- F&O
def _index_option_code(symbol: str, rank: int) -> int:
    """MarketCode for an index option by underlying and expiry rank (0 = nearest).

    NIFTY/BANKNIFTY: nearest four expiries -> 7, 8, 9, 14 (the .NET 1W..4W buckets);
    FINNIFTY / MIDCPNIFTY: nearest -> 11 / 12; everything else -> 1.
    This is OUR mapping - the .NET token server's exact bucketing is not in the source.
    """
    if symbol in ("NIFTY", "BANKNIFTY") and rank < 4:
        return (7, 8, 9, 14)[rank]
    if symbol == "FINNIFTY" and rank == 0:
        return 11
    if symbol == "MIDCPNIFTY" and rank == 0:
        return 12
    return 1


def convert_fo(path: Path, stats: Counter) -> tuple[list[dict], set[str]]:
    rows = read_master(path, FO_POS, ("token", "instrument", "symbol", "expiry", "strike", "option", "board_lot"),
                       csv_positions=FO_CSV_POS)
    _drop_unvalidated_delete_flag(rows)
    parsed, bad = [], []
    for r in rows:
        try:
            if r.get("instrument", "") not in FO_TYPES:
                stats["fo_skipped_other_instrument"] += 1
                continue
            rec = {"token": _int(r["token"]), "instrument": r["instrument"], "symbol": r["symbol"],
                   "expiry": _expiry(r["expiry"]), "strike": _int(r["strike"]), "option": r["option"],
                   "lot": _int(r["board_lot"]), "tick": _int(r["tick"]) if r.get("tick") else 0,
                   "base": _opt_int(r.get("base_price")), "name": r.get("name", ""), "_active": _active(r)}
            if rec["token"] <= 0 or rec["lot"] < 1 or not rec["symbol"]:
                raise ValueError("token/lot/symbol out of range")
            parsed.append(rec)
        except (KeyError, ValueError) as exc:
            bad.append(f"{exc} | {r['_raw']}")
    _check_quality(str(path), len(parsed), bad)

    named = [p for p in parsed if p["name"]]
    stats["fo_name_mismatch"] += sum(1 for p in named if not p["name"].upper().startswith(p["symbol"].upper()))
    active = [p for p in parsed if p["_active"]]
    stats["fo_inactive_skipped"] += len(parsed) - len(active)
    expiries: dict[tuple[str, str], list[date]] = defaultdict(list)
    for p in active:
        expiries[(p["instrument"], p["symbol"])].append(p["expiry"])
    rank = {k: {e: i for i, e in enumerate(sorted(set(v)))} for k, v in expiries.items()}

    out, fo_symbols = [], set()
    for p in active:
        inst, sym, exp = p["instrument"], p["symbol"], p["expiry"]
        rk = rank[(inst, sym)][exp]
        if inst == "FUTSTK":
            code = 0
        elif inst == "FUTIDX":
            code = 5 if rk == 0 else 6 if rk == 1 else None
        elif inst == "OPTSTK":
            code = 2
        else:
            code = _index_option_code(sym, rk)
        if code is None:
            stats["fo_far_index_future_skipped"] += 1
            continue
        if inst in ("FUTSTK", "OPTSTK"):
            fo_symbols.add(sym)
        is_opt = inst.startswith("OPT")
        strike = p["strike"] / 100 if is_opt and p["strike"] > 0 else ""
        opt = p["option"] if is_opt else ""
        name = f"{sym}{exp:%d%b%y}".upper() + (f"{strike:g}{opt}" if is_opt else "FUT")
        out.append({"ScriptCode": p["token"], "Symbol": sym, "ScriptName": name, "Series": inst,
                    "LotSize": p["lot"], "Close": p["base"] / 100 if p["base"] > 0 else 0, "Volume": 0, "OI": 0,
                    "Nifty": "NA", "Future": "F", "Sector": "", "Price_Jump": p["tick"] / 100 if p["tick"] else 0,
                    "Strike_Price": strike, "Option_Type": opt, "Expiry": exp.isoformat(), "MarketCode": code})
        stats[f"fo_code_{code}"] += 1
    return out, fo_symbols


# ----------------------------------------------------------------------------- cash
def convert_cm(path: Path, fo_symbols: set[str], stats: Counter) -> list[dict]:
    rows = read_master(path, CM_POS, ("token", "symbol", "series"), csv_positions=CM_CSV_POS)
    out, bad, seen = [], [], 0
    for r in rows:
        try:
            tok = _int(r["token"])
            if tok <= 0 or not r["symbol"] or not r["series"]:
                raise ValueError("token/symbol/series out of range")
            seen += 1
        except (KeyError, ValueError) as exc:
            bad.append(f"{exc} | {r['_raw']}")
            continue
        series = r["series"]
        if series not in ("EQ", "SM"):
            stats["cm_other_series_skipped"] += 1
            continue
        if not _active(r):
            stats["cm_inactive_skipped"] += 1
            continue
        has_fut = r["symbol"] in fo_symbols
        code = 13 if series == "SM" else 4 if has_fut else 3
        lot = _int(r["board_lot"]) if r.get("board_lot", "").strip().isdigit() else 1
        out.append({"ScriptCode": tok, "Symbol": r["symbol"], "ScriptName": r["symbol"], "Series": series,
                    "LotSize": max(lot, 1), "Close": 0, "Volume": 0, "OI": 0, "Nifty": "NA",
                    "Future": "F" if has_fut else "NA", "Sector": "",
                    "Price_Jump": _int(r["tick"]) / 100 if r.get("tick", "").strip().isdigit() else 0,
                    "Strike_Price": "", "Option_Type": "", "Expiry": "", "MarketCode": code})
        stats[f"cm_code_{code}"] += 1
    _check_quality(str(path), seen, bad)
    return out


def index_rows(indices: dict[str, int]) -> list[dict]:
    return [{"ScriptCode": code, "Symbol": name.upper(), "ScriptName": name, "Series": "EQ", "LotSize": 1,
             "Close": 0, "Volume": 0, "OI": 0, "Nifty": "NA", "Future": "F", "Sector": "Sectoral Indices",
             "Price_Jump": 0.05, "Strike_Price": "", "Option_Type": "", "Expiry": "", "MarketCode": 10}
            for name, code in indices.items()]


# ----------------------------------------------------------------------------- driver
def write_meta(out_path: Path, rows: list[dict], stats: Counter, fo: Path | None, cm: Path | None) -> Path:
    """Sidecar ``<csv>.meta.json`` so consumers can check freshness without parsing the CSV."""
    meta = {"generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rows": len(rows),
            "per_market_code": dict(sorted(Counter(r["MarketCode"] for r in rows).items())),
            "sources": {"fo": Path(fo).name if fo else None, "cm": Path(cm).name if cm else None},
            "columns": list(OUT_COLUMNS), "notes": dict(sorted(stats.items()))}
    path = out_path.with_name(out_path.name + ".meta.json")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def build(fo: Path | None, cm: Path | None, out_path: Path | None, indices: dict[str, int],
          dry_run: bool = False, bhav: dict | None = None) -> tuple[list[dict], Counter]:
    """``bhav`` (optional): {"cm": {...}, "fo": {...}, "idx": {...}} as returned by the bhav readers."""
    if fo is None and cm is None:
        raise NseImportError("give at least one of --fo / --cm")
    stats: Counter = Counter()
    rows: list[dict] = []
    fo_symbols: set[str] = set()
    if fo:
        fo_rows, fo_symbols = convert_fo(Path(fo), stats)
        rows += fo_rows
    if cm:
        rows += convert_cm(Path(cm), fo_symbols, stats)
        rows += index_rows(indices)
    seen: set[int] = set()
    unique = []
    for r in rows:                                    # a token must be unique across files
        if r["ScriptCode"] in seen:
            stats["duplicate_token_dropped"] += 1
            continue
        seen.add(r["ScriptCode"])
        unique.append(r)
    if not unique:
        raise NseImportError("no usable rows were produced")
    if bhav:
        from . import bhav as nb
        nb.apply(unique, bhav.get("cm"), bhav.get("fo"), bhav.get("idx"), stats)
    if not dry_run and out_path is not None:
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
            w.writeheader()
            w.writerows(unique)
        os.replace(tmp, out_path)
        write_meta(out_path, unique, stats, fo, cm)
    return unique, stats
