"""Terminal output and logging shared by every command.

One Rich console draws progress bars and prints warnings, so a warning raised during a
long job appears above the bar instead of breaking it. Everything is also written to
`data/crawl/crawl.log` (rotated at 10 MB), including a progress line once a minute, so a
job started in the background can be followed with `tail -f data/crawl/crawl.log`.
"""

from __future__ import annotations

import logging
import logging.handlers
import signal
import time

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from parts_crawler.config import Settings

console = Console()
log = logging.getLogger("parts_crawler")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class ConsoleHandler(logging.Handler):
    """Warnings and errors as one short line each; tracebacks stay in the log file."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            style = "red" if record.levelno >= logging.ERROR else "yellow"
            console.print(f"[{style}]{record.levelname.lower()}:[/] "
                          f"{escape(record.getMessage())}", highlight=False, soft_wrap=True)
        except Exception:
            self.handleError(record)


def configure_logging(settings: Settings) -> None:
    """Attach the rotating file log and the console handler once per process."""
    root = logging.getLogger("parts_crawler")
    if any(isinstance(handler, logging.handlers.RotatingFileHandler)
           for handler in root.handlers):
        return
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = logging.handlers.RotatingFileHandler(
        settings.log_path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    screen = ConsoleHandler()
    screen.setLevel(logging.WARNING)
    root.addHandler(screen)
    # Third-party request logs could include query strings; keep them out.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def stop_on_sigterm() -> None:
    """Make `kill <pid>` and a closed terminal stop a job the way Ctrl+C does.

    The job then finishes or rolls back the unit of work in flight and exits; running
    the same command again continues from there.
    """
    def handler(_signum, _frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handler)
    # Under nohup the hangup signal is ignored on purpose; leave that alone.
    if hasattr(signal, "SIGHUP") and signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN:
        signal.signal(signal.SIGHUP, handler)


def span(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


class Tracker:
    """Progress of one long job: a live bar on a terminal, a line every 30 s otherwise.

    `done` may start above zero when a job resumes from a checkpoint; the rate and the
    remaining time are computed from the work done in this run only.
    """

    def __init__(self, description: str, total: int | None = None, *, unit: str = "rows",
                 done: int = 0, logger: logging.Logger | None = None) -> None:
        self.description = description
        self.total = total
        self.unit = unit
        self.done = done
        self.log = logger or log
        self._resumed_at = done
        self._started = time.monotonic()
        self._printed = self._started
        self._logged = self._started
        self._progress: Progress | None = None
        self._task = None

    def __enter__(self) -> Tracker:
        if console.is_terminal:
            self._progress = Progress(
                SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                BarColumn(), MofNCompleteColumn(), TaskProgressColumn(),
                TextColumn("[dim]{task.fields[rate]}"), TimeElapsedColumn(),
                TimeRemainingColumn(), console=console)
            self._progress.start()
            self._task = self._progress.add_task(
                self.description, total=self.total, completed=self.done, rate="")
        if self._resumed_at:
            self.log.info("%s: continuing at %s", self.description, self._position())
        else:
            self.log.info("%s: started (%s)", self.description, self._position())
        return self

    def __exit__(self, kind, _error, _traceback) -> bool:
        if self._progress is not None:
            self._progress.stop()
        outcome = ("finished" if kind is None
                   else "stopped" if kind in {KeyboardInterrupt, SystemExit} else "failed")
        line = (f"{self.description}: {outcome} at {self._position()} "
                f"after {span(time.monotonic() - self._started)}")
        # A warning reaches the terminal through the console handler; a normal finish
        # leaves the completed bar on screen and only needs the log entry.
        self.log.log(logging.INFO if kind is None else logging.WARNING, line)
        if kind is None and not console.is_terminal:
            console.print(line)
        return False

    def _position(self) -> str:
        if self.total:
            return (f"{self.done:,} of {self.total:,} {self.unit} "
                    f"({self.done * 100 // max(self.total, 1)}%)")
        return f"{self.done:,} {self.unit}"

    def _rate(self) -> str:
        elapsed = max(time.monotonic() - self._started, 1e-6)
        per_second = (self.done - self._resumed_at) / elapsed
        if per_second >= 1:
            return f"{per_second:,.0f} {self.unit}/s"
        return f"{per_second * 60:,.1f} {self.unit}/min"

    def advance(self, amount: int = 1, *, total: int | None = None,
                description: str | None = None) -> None:
        self.done += amount
        if total is not None:
            self.total = total
        if description is not None:
            self.description = description
        now = time.monotonic()
        if self._progress is not None:
            self._progress.update(self._task, completed=self.done, total=self.total,
                                  description=self.description, rate=self._rate())
        elif now - self._printed >= 30:
            self._printed = now
            console.print(f"[{time.strftime('%H:%M:%S')}] {self.description}: "
                          f"{self._position()} {self._rate()}")
        if now - self._logged >= 60:
            self._logged = now
            self.log.info("%s: %s, %s", self.description, self._position(), self._rate())
