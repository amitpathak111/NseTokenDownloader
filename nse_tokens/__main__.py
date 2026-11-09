"""Command line.

    python -m nse_tokens update            # download today's NSE files + build the shared CSV  (daily job)
    python -m nse_tokens download          # download only
    python -m nse_tokens import --fo F --cm C [--dry-run]   # build the CSV from files you downloaded yourself
    python -m nse_tokens fetch-server --url URL              # build the CSV from an internal token server
    python -m nse_tokens info              # show age / size of the current CSV
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import bhav as nb
from . import download as dl
from . import importer as ni
from .config import Config, ConfigError, load_config
from .fetch_server import FetchError, fetch_to_csv

log = logging.getLogger("nse_tokens")


def _day(cfg: Config, text: str | None):
    from zoneinfo import ZoneInfo
    return datetime.strptime(text, "%Y-%m-%d").date() if text else datetime.now(ZoneInfo(cfg.timezone)).date()


def _indices(cfg: Config, args) -> dict[str, int]:
    out = {} if getattr(args, "no_default_indices", False) else dict(cfg.indices)
    for spec in getattr(args, "index", None) or []:
        name, _, code = spec.rpartition("=")
        if not name or not code.strip().isdigit():
            raise ValueError(f'bad --index {spec!r}; use NAME=CODE e.g. "Nifty 50=900001"')
        out[name.strip()] = int(code)
    return out


def _download(cfg: Config, day) -> tuple[Path, Path]:
    cm = dl.download_one(cfg.cm_url, day, cfg.download_dir, cfg.timeout_sec, cfg.lookback_days)
    fo = dl.download_one(cfg.fo_url, day, cfg.download_dir, cfg.timeout_sec, cfg.lookback_days)
    return cm, fo


def _bhav(cfg: Config, day, args=None) -> tuple[dict | None, dict]:
    """Download + read the bhavcopies. A missing one is a loud warning, not a failure: tokens still build."""
    if not cfg.bhav_enabled or getattr(args, "no_bhav", False):
        return None, {}
    data, files = {}, {}
    for key, url, reader in (("cm", cfg.bhav_cm_url, lambda p: nb.read_bhav(p)),
                             ("fo", cfg.bhav_fo_url, lambda p: nb.read_bhav(p, cfg.bhav_fo_close)),
                             ("idx", cfg.bhav_index_url, nb.read_index_close)):
        try:
            path = dl.download_one(url, day, cfg.download_dir, cfg.timeout_sec, cfg.lookback_days)
            data[key], files[key] = reader(path), path.name
        except (dl.DownloadError, ni.NseImportError) as exc:
            log.warning("bhavcopy %s NOT applied - Close/Volume/OI stay at master-file values: %s", key, exc)
    return (data or None), files


def _report(rows, stats, out: Path | None, dry: bool) -> None:
    print(f"{'DRY RUN - nothing written' if dry else 'wrote ' + str(out)}: {len(rows)} tokens")
    for k in sorted(stats):
        print(f"  {k:<32}{stats[k]}")
    seen = set()
    print("sample rows:")
    for r in rows:
        if r["MarketCode"] not in seen:
            seen.add(r["MarketCode"])
            print("  ", {k: r[k] for k in ("ScriptCode", "ScriptName", "Series", "LotSize", "Close", "Volume", "OI", "Strike_Price",
                                            "Option_Type", "Expiry", "MarketCode")})


def cmd_download(cfg: Config, args) -> int:
    day = _day(cfg, args.date)
    cm, fo = _download(cfg, day)
    print(f"cash : {cm}\nF&O  : {fo}")
    if cfg.bhav_enabled:
        for url in (cfg.bhav_cm_url, cfg.bhav_fo_url, cfg.bhav_index_url):
            try:
                print("bhav :", dl.download_one(url, day, cfg.download_dir, cfg.timeout_sec, cfg.lookback_days))
            except dl.DownloadError as exc:
                print("bhav : NOT downloaded -", exc)
    return 0


def cmd_update(cfg: Config, args) -> int:
    day = _day(cfg, args.date)
    cm, fo = _download(cfg, day)
    bhav, files = _bhav(cfg, day, args)
    rows, stats = ni.build(fo, cm, cfg.output_csv, _indices(cfg, args), bhav=bhav)
    if files:
        print("bhavcopy:", files)
    _report(rows, stats, cfg.output_csv, False)
    return 0


def cmd_import(cfg: Config, args) -> int:
    bhav = {}
    if args.bhav_cm:
        bhav["cm"] = nb.read_bhav(Path(args.bhav_cm))
    if args.bhav_fo:
        bhav["fo"] = nb.read_bhav(Path(args.bhav_fo), cfg.bhav_fo_close)
    if args.bhav_index:
        bhav["idx"] = nb.read_index_close(Path(args.bhav_index))
    rows, stats = ni.build(args.fo, args.cm, cfg.output_csv, _indices(cfg, args), dry_run=args.dry_run,
                           bhav=bhav or None)
    _report(rows, stats, cfg.output_csv, args.dry_run)
    return 0


def cmd_fetch(cfg: Config, args) -> int:
    n = fetch_to_csv(args.url, cfg.output_csv, cfg.timeout_sec)
    print(f"wrote {n} tokens to {cfg.output_csv}")
    return 0


def cmd_info(cfg: Config, args) -> int:
    meta = cfg.output_csv.with_name(cfg.output_csv.name + ".meta.json")
    if not cfg.output_csv.exists():
        print(f"{cfg.output_csv} does not exist yet - run: python -m nse_tokens update")
        return 1
    print(f"file     : {cfg.output_csv}  ({cfg.output_csv.stat().st_size:,} bytes)")
    if meta.exists():
        m = json.loads(meta.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(m["generated_at_utc"])
        print(f"generated: {m['generated_at_utc']}  ({age.total_seconds() / 3600:.1f} h ago)")
        print(f"rows     : {m['rows']}   sources: {m['sources']}")
        print(f"by market code: {m['per_market_code']}")
    else:
        print("no .meta.json next to it (file was not produced by `update`/`import`)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nse_tokens", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="path to config.yaml (default: ./config.yaml or $NSE_TOKENS_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_date(sp):
        sp.add_argument("--date", help="YYYY-MM-DD (default: today); falls back to earlier trading days")

    def add_index(sp):
        sp.add_argument("--index", action="append", metavar="NAME=CODE", help="extra/override cash index row")
        sp.add_argument("--no-default-indices", action="store_true")

    add_date(sub.add_parser("download", help="download NSE cash + F&O master files"))
    u = sub.add_parser("update", help="download and build the shared CSV")
    add_date(u); add_index(u)
    u.add_argument("--no-bhav", action="store_true", help="skip bhavcopy (Close/Volume/OI left as in master files)")
    i = sub.add_parser("import", help="build the CSV from local NSE files")
    i.add_argument("--fo", help="NSE_FO_contract_ddmmyyyy.csv.gz (or contract.gz/.txt)")
    i.add_argument("--cm", help="NSE_CM_security_ddmmyyyy.csv.gz (or security.gz/.txt)")
    i.add_argument("--bhav-cm", help="local BhavCopy_NSE_CM_*.csv.zip")
    i.add_argument("--bhav-fo", help="local BhavCopy_NSE_FO_*.csv.zip")
    i.add_argument("--bhav-index", help="local ind_close_all_*.csv")
    i.add_argument("--dry-run", action="store_true", help="validate and summarise, write nothing")
    add_index(i)
    f = sub.add_parser("fetch-server", help="build the CSV from an internal token server (JSON)")
    f.add_argument("--url", required=True, help="e.g. http://HOST:8060/tokens/findall")
    sub.add_parser("info", help="show the current CSV's age and contents")

    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config)
        logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)-7s %(message)s")
        return {"download": cmd_download, "update": cmd_update, "import": cmd_import,
                "fetch-server": cmd_fetch, "info": cmd_info}[args.cmd](cfg, args)
    except (ConfigError, ni.NseImportError, FetchError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except dl.DownloadError as exc:
        print(f"download failed: {exc}", file=sys.stderr)
        print("Fallback: download both files in a browser (see README) and run `import --fo ... --cm ...`.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
