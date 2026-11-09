"""NSE bhavcopy -> previous close / volume / open interest for the token CSV.

Files (all published after end of day, same holiday/weekend fallback as the master files):
  * cash  UDiFF bhavcopy  BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
  * F&O   UDiFF bhavcopy  BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip
  * index closes          ind_close_all_DDMMYYYY.csv
Rows are joined to the master files by exchange token (UDiFF ``FinInstrmId``); indices by name.
Columns are located by header name, so a re-ordered file still works and a renamed one fails loudly.
"""
from __future__ import annotations

import csv
import io
import zipfile
from collections import Counter
from pathlib import Path

from .importer import NseImportError

CLOSE_FIELDS = {"close": "ClsPric", "settlement": "SttlmPric"}


def _text(path: Path) -> str:
    raw = Path(path).read_bytes()
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise NseImportError(f"{path}: zip contains no .csv")
            raw = z.read(names[0])
    return raw.decode("utf-8-sig", errors="replace")


def _reader(path: Path, required: tuple[str, ...]) -> tuple[csv.DictReader, dict[str, str]]:
    rd = csv.DictReader(io.StringIO(_text(path)))
    cols = {(c or "").strip().lower(): c for c in (rd.fieldnames or [])}
    missing = [c for c in required if c.lower() not in cols]
    if missing:
        raise NseImportError(f"{path}: missing column(s) {missing}; file has {list(cols.values())[:40]}")
    return rd, cols


def _num(v: str | None) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def read_bhav(path: Path, close_field: str = "close") -> dict[int, dict[str, float]]:
    """token -> {close, volume, oi} from a UDiFF CM or FO bhavcopy."""
    cf = CLOSE_FIELDS[close_field]
    rd, cols = _reader(path, ("FinInstrmId", cf))
    g = lambda row, name: row.get(cols.get(name.lower(), ""), "")          # noqa: E731
    out: dict[int, dict[str, float]] = {}
    bad = total = 0
    for row in rd:
        total += 1
        tok, close = _num(g(row, "FinInstrmId")), _num(g(row, cf))
        if tok is None or close is None or close < 0:
            bad += 1
            continue
        out[int(tok)] = {"close": close, "volume": int(_num(g(row, "TtlTradgVol")) or 0),
                         "oi": int(_num(g(row, "OpnIntrst")) or 0)}
    if total == 0 or bad > total * 0.05:
        raise NseImportError(f"{path}: {bad} of {total} rows unusable - not the expected UDiFF bhavcopy layout")
    return out


def read_index_close(path: Path) -> dict[str, float]:
    """lower-cased index name -> closing value from ind_close_all_DDMMYYYY.csv."""
    rd, cols = _reader(path, ("Index Name", "Closing Index Value"))
    out = {}
    for row in rd:
        v = _num(row.get(cols["closing index value"]))
        if v is not None:
            out[row[cols["index name"]].strip().lower()] = v
    if not out:
        raise NseImportError(f"{path}: no index rows")
    return out


def apply(rows: list[dict], cm: dict[int, dict] | None, fo: dict[int, dict] | None,
          idx: dict[str, float] | None, stats: Counter) -> None:
    """Fill Close / Volume / OI in place. A token with no bhav row keeps its old values and is counted."""
    for r in rows:
        if r["MarketCode"] == 10:
            if idx is None:
                continue
            v = idx.get(str(r["ScriptName"]).lower())
            if v is None:
                stats["bhav_unmatched_index"] += 1
            else:
                r["Close"] = v
                stats["bhav_matched_index"] += 1
            continue
        is_fo = r["Series"] in ("FUTSTK", "FUTIDX", "OPTSTK", "OPTIDX")
        src = fo if is_fo else cm
        if src is None:
            continue
        b = src.get(int(r["ScriptCode"]))
        tag = "fo" if is_fo else "cm"
        if b is None:
            stats[f"bhav_unmatched_{tag}"] += 1        # e.g. contract that did not trade / new listing
            continue
        r["Close"], r["Volume"] = b["close"], b["volume"]
        if is_fo:
            r["OI"] = b["oi"]
        stats[f"bhav_matched_{tag}"] += 1
