"""Configuration for nse_tokens (``config.yaml`` next to the package)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CM = "https://nsearchives.nseindia.com/content/cm/NSE_CM_security_{DDMMYYYY}.csv.gz"
DEFAULT_FO = "https://nsearchives.nseindia.com/content/fo/NSE_FO_contract_{DDMMYYYY}.csv.gz"
DEFAULT_BHAV_CM = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip"
DEFAULT_BHAV_FO = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip"
DEFAULT_BHAV_IDX = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{DDMMYYYY}.csv"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    path: Path
    output_csv: Path            # the shared file other projects read
    download_dir: Path
    cm_url: str
    fo_url: str
    lookback_days: int
    timeout_sec: float
    timezone: str
    indices: dict[str, int]     # cash-index rows added to the CSV: name -> ScriptCode
    log_level: str
    bhav_enabled: bool = True
    bhav_cm_url: str = DEFAULT_BHAV_CM
    bhav_fo_url: str = DEFAULT_BHAV_FO
    bhav_index_url: str = DEFAULT_BHAV_IDX
    bhav_fo_close: str = "close"        # "close" (ClsPric) or "settlement" (SttlmPric)


def _need(d: dict, key: str, typ, default=None):
    v = d.get(key, default)
    if v is None:
        raise ConfigError(f"{key}: required setting is missing")
    if isinstance(v, bool) or not isinstance(v, typ):
        raise ConfigError(f"{key}: expected {getattr(typ, '__name__', typ)}, got {v!r}")
    return v


def _path(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def load_config(explicit: str | os.PathLike | None = None) -> Config:
    p = Path(explicit or os.environ.get("NSE_TOKENS_CONFIG") or "config.yaml").expanduser().resolve()
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: invalid YAML: {exc}") from exc
    base = p.parent
    urls = {}
    for key, default in (("cm_url", DEFAULT_CM), ("fo_url", DEFAULT_FO)):
        u = _need(raw, key, str, default)
        if not u.lower().startswith(("http://", "https://")) or "{DDMMYYYY}" not in u:
            raise ConfigError(f"{key}: must be an http(s) URL containing {{DDMMYYYY}}, got {u!r}")
        urls[key] = u
    idx = raw.get("indices", {"Nifty 50": 900001, "Nifty Bank": 900002})
    if not isinstance(idx, dict) or not all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
                                            for k, v in idx.items()):
        raise ConfigError("indices: expected a mapping of index name -> integer ScriptCode")
    lookback = _need(raw, "lookback_days", int, 7)
    if not 0 <= lookback <= 30:
        raise ConfigError("lookback_days: must be 0-30")
    tz = _need(raw, "timezone", str, "Asia/Kolkata")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"timezone: unknown timezone {tz!r} (Windows: pip install tzdata)") from exc
    level = _need(raw, "log_level", str, "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("log_level: must be DEBUG, INFO, WARNING or ERROR")
    bh = raw.get("bhav", {}) or {}
    if not isinstance(bh, dict):
        raise ConfigError("bhav: expected a mapping")
    enabled = bh.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("bhav.enabled: expected true/false")
    burls = {}
    for key, default in (("cm_url", DEFAULT_BHAV_CM), ("fo_url", DEFAULT_BHAV_FO), ("index_url", DEFAULT_BHAV_IDX)):
        u = _need(bh, key, str, default)
        if not u.lower().startswith(("http://", "https://")) or not ("{DDMMYYYY}" in u or "{YYYYMMDD}" in u):
            raise ConfigError(f"bhav.{key}: must be an http(s) URL containing {{YYYYMMDD}} or {{DDMMYYYY}}, got {u!r}")
        burls[key] = u
    fo_close = _need(bh, "fo_close", str, "close")
    if fo_close not in ("close", "settlement"):
        raise ConfigError("bhav.fo_close: must be 'close' or 'settlement'")
    return Config(p, _path(base, _need(raw, "output_csv", str)),
                  _path(base, _need(raw, "download_dir", str, "downloads")), urls["cm_url"], urls["fo_url"],
                  lookback, float(_need(raw, "timeout_sec", (int, float), 30)), tz, dict(idx), level,
                  enabled, burls["cm_url"], burls["fo_url"], burls["index_url"], fo_close)
