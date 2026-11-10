"""Bulk importers for downloadable datasets, with shared download and resume helpers.

Every importer follows the same rules so a run can be stopped at any moment (Ctrl+C,
`kill`, a closed terminal, a lost connection) and continued by running the same
command again: downloads resume from the bytes already on disk, each batch is committed
together with its checkpoint, and all writes are upserts.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import requests
from rich.progress import BarColumn, DownloadColumn, Progress, TimeRemainingColumn

from parts_crawler.store import SCHEMA_HINT, Store
from parts_crawler.term import console

log = logging.getLogger("parts_crawler.importers")


def require_schema(store: Store) -> None:
    if not store.schema_ready():
        raise SystemExit(SCHEMA_HINT)


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: Path, *, user_agent: str, attempts: int = 4,
             refresh: bool = False) -> Path:
    """Download with retries and a progress bar.

    A finished file is reused unless `refresh` is set. An interrupted download keeps its
    `.part` file and continues from there when the server supports byte ranges; the
    saved validator (ETag or Last-Modified) makes the server restart the transfer if
    the remote file changed in the meantime.
    """
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists() and target.stat().st_size > 0 and not refresh:
        log.info("download %s: reusing the file already on disk", target.name)
        return target
    partial = target.with_suffix(target.suffix + ".part")
    validator_file = target.with_suffix(target.suffix + ".part.validator")
    for attempt in range(1, attempts + 1):
        have = partial.stat().st_size if partial.exists() else 0
        validator = validator_file.read_text().strip() if validator_file.exists() else ""
        headers = {"User-Agent": user_agent}
        if have and validator:
            headers.update({"Range": f"bytes={have}-", "If-Range": validator})
        else:
            have = 0
        try:
            with requests.get(url, stream=True, timeout=(10, 120),
                              headers=headers) as response:
                if response.status_code == 416:
                    # The kept part is already the whole file, or no longer matches the
                    # remote one. Either way the range is useless: start this file again.
                    partial.unlink(missing_ok=True)
                    validator_file.unlink(missing_ok=True)
                    raise requests.ConnectionError("stale partial download discarded")
                response.raise_for_status()
                resumed = have > 0 and response.status_code == 206
                if not resumed:
                    have = 0
                validator_file.write_text(response.headers.get("ETag")
                                          or response.headers.get("Last-Modified") or "")
                # A compressed transfer reports the compressed length, which cannot be
                # compared with the bytes written, so no total is known in that case.
                length = 0 if response.headers.get("Content-Encoding") else int(
                    response.headers.get("content-length") or 0)
                total = have + length if length else None
                if resumed:
                    log.info("download %s: continuing at byte %s", target.name, have)
                with Progress("[progress.description]{task.description}", BarColumn(),
                              DownloadColumn(), TimeRemainingColumn(),
                              console=console) as progress:
                    task = progress.add_task(f"download {target.name}", total=total,
                                             completed=have)
                    with partial.open("ab" if resumed else "wb") as handle:
                        for chunk in response.iter_content(1 << 20):
                            handle.write(chunk)
                            progress.advance(task, len(chunk))
            if total and partial.stat().st_size != total:
                raise requests.ConnectionError("connection closed before the file was complete")
            partial.replace(target)
            validator_file.unlink(missing_ok=True)
            log.info("download %s: complete, %s bytes", target.name, target.stat().st_size)
            return target
        except requests.RequestException as error:
            kept = partial.stat().st_size if partial.exists() else 0
            log.warning("download %s: attempt %s of %s failed (%s); %s bytes kept",
                        target.name, attempt, attempts, type(error).__name__, kept)
            if attempt == attempts:
                raise SystemExit(f"Download failed after {attempts} attempts: {url}. "
                                 "Run the command again to continue it.") from None
            time.sleep(5 * attempt)
    return target
