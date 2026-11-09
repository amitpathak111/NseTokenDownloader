"""Optional: pull tokens from an internal token server (JSON array) instead of NSE files."""
from __future__ import annotations

import csv
import json
import os
import urllib.request
from pathlib import Path

from .importer import CSV_COLUMNS


class FetchError(RuntimeError):
    pass


def fetch_to_csv(url: str, out_path: Path, timeout: float = 15.0) -> int:
    """Download a JSON array of token objects and write the shared CSV (atomically). Returns row count."""
    if not url.lower().startswith(("http://", "https://")):
        raise FetchError(f"source must be an http(s) URL, got {url!r}")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:      # noqa: S310 - scheme checked above
            data = json.loads(resp.read().decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise FetchError(f"could not download tokens from {url}: {exc}") from exc
    if not isinstance(data, list) or not data or not all(isinstance(r, dict) for r in data):
        raise FetchError(f"{url}: expected a non-empty JSON array of token objects")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for r in data:
            w.writerow(["" if r.get(c) is None else r.get(c) for c in CSV_COLUMNS])
    os.replace(tmp, out_path)
    return len(data)
