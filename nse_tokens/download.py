"""Download NSE's daily master files (cash security + F&O contract)."""
from __future__ import annotations

import gzip
import logging
import os
import time
import urllib.error
import zipfile
import io
import zlib
import urllib.request
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
MIN_BYTES = 200
RETRIES = 3
POLITE_DELAY_SEC = 1.0


class DownloadError(RuntimeError):
    pass


class NotAvailable(DownloadError):
    """The server answered with 404/403: no file for that date (holiday, not published yet) - or, for 403, blocked."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _fetch(url: str, timeout: float) -> bytes:
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 - scheme validated in config
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                # NSE answers 403 for "no such file" on its archive host as well as for blocked clients.
                raise NotAvailable(f"HTTP {exc.code} for {url}", exc.code) from exc
            last = exc
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        if attempt < RETRIES:
            time.sleep(attempt * 2)
    raise DownloadError(f"could not reach {url}: {last}") from last


def _validate_gzip(data: bytes, url: str) -> None:
    if len(data) < MIN_BYTES or data[:2] != b"\x1f\x8b":
        raise DownloadError(f"{url} did not return a gzip file (got {len(data)} bytes starting {data[:40]!r}) - "
                            "NSE may be blocking this client; download the file in a browser instead")
    try:                                       # full decompress verifies length + CRC, so truncation is caught
        if not gzip.decompress(data)[:1]:
            raise DownloadError(f"{url}: gzip file is empty")
    except (OSError, EOFError, zlib.error) as exc:
        raise DownloadError(f"{url}: corrupt or truncated gzip data ({exc})") from exc


def _validate_zip(data: bytes, url: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            bad = z.testzip()
            if bad or not z.namelist():
                raise DownloadError(f"{url}: zip is empty or member {bad!r} is corrupt")
    except zipfile.BadZipFile as exc:
        raise DownloadError(f"{url} did not return a zip file (got {len(data)} bytes starting {data[:40]!r}) - "
                            "NSE may be blocking this client") from exc


def _validate_text(data: bytes, url: str) -> None:
    if len(data) < MIN_BYTES or data.lstrip()[:1] == b"<":
        raise DownloadError(f"{url}: not a CSV (got {len(data)} bytes starting {data[:40]!r}) - "
                            "NSE may be blocking this client")


def _validate(data: bytes, url: str) -> None:
    low = url.lower()
    (_validate_gzip if low.endswith(".gz") else _validate_zip if low.endswith(".zip") else _validate_text)(data, url)


def download_one(url_template: str, day: date, dest_dir: Path, timeout: float, lookback: int) -> Path:
    """Download the newest available file at or before ``day``; returns the saved path.

    Every calendar day is tried, weekends included: NSE holds special sessions on weekends (Budget day,
    Diwali Muhurat trading) and publishes files for them. NSE itself answers 404/403 for days without a
    file, so no holiday calendar is needed. A warning is logged when the file used is not ``day``'s.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    tried: list[str] = []
    saw_403 = False
    for back in range(lookback + 1):
        d = day - timedelta(days=back)
        url = url_template.replace("{DDMMYYYY}", f"{d:%d%m%Y}").replace("{YYYYMMDD}", f"{d:%Y%m%d}")
        target = dest_dir / url.rsplit("/", 1)[-1]
        if target.exists() and target.stat().st_size >= MIN_BYTES:
            log.info("already downloaded: %s", target)
            _warn_if_older(d, day, target)
            return target
        try:
            data = _fetch(url, timeout)
            _validate(data, url)
        except NotAvailable as exc:
            saw_403 = saw_403 or exc.code == 403
            tried.append(f"{d:%d-%b-%Y}")
            time.sleep(POLITE_DELAY_SEC)
            continue
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        log.info("downloaded %s (%d bytes)", target, len(data))
        _warn_if_older(d, day, target)
        return target
    hint = ("NSE answers HTTP 403 both for a missing file and for a blocked client, so this is either no file "
            "for those days or NSE is blocking this machine - try opening the URL in a browser. "
            if saw_403 else "")
    raise NotAvailable(f"no file found for {', '.join(tried)} ({url_template}). {hint}"
                       "Files are published after end of day on days the exchange traded; "
                       "raise lookback_days for long closures.")


def _warn_if_older(found: date, wanted: date, target: Path) -> None:
    if found != wanted:
        log.warning("using %s dated %s, NOT %s - no file for %s (holiday, closed day, or not published yet; "
                    "NSE publishes after end of day). This is the latest available.",
                    target.name, f"{found:%d-%b-%Y}", f"{wanted:%d-%b-%Y}", f"{wanted:%d-%b-%Y}")
