#!/usr/bin/env python3
"""Vendor and generate the static assets of the public site.

Everything under ``automotive/static/site`` is served as it is on disk: the site has
no Node build step.  This script is the one place that touches the network or a
JavaScript toolchain, and it is run by hand when a pinned version or the engine
model changes.

    python3 -I scripts/vendor_site_assets.py vendor    # three.js into site/vendor/
    python3 -I scripts/vendor_site_assets.py check     # hashes, import graph, CSP, node --check
    python3 -I scripts/vendor_site_assets.py burst     # img/engine/burst.webp
    python3 -I scripts/vendor_site_assets.py posters   # img/engine/poster-*.webp and pins.json

Only the Python standard library is used.  External tools: ``npx`` (Node 22, for the
pinned esbuild), ``node``, ``google-chrome`` and ``magick`` (ImageMagick 7).

To vendor another npm package, add an ``NpmAsset`` to ``ASSETS``.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import functools
import hashlib
import hmac
import http.server
import io
import json
import math
import os
import random
import re
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_ROOT = REPO_ROOT / "automotive" / "static" / "site"
REGISTRY = "https://registry.npmjs.org"
ESBUILD = "esbuild@0.28.2"
MANIFEST_NAME = "VENDOR.json"


class AssetError(RuntimeError):
    """A vendoring or generation step failed and nothing should be trusted."""


# --------------------------------------------------------------------------------------
# Third-party code
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VendorFile:
    """One file taken out of an npm tarball."""

    member: str  # path inside the package, e.g. "build/three.module.js"
    output: str  # file name inside the asset's output directory
    minify: bool = False
    # (bare specifier, relative replacement) pairs applied to import statements.
    rewrites: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class NpmAsset:
    """A pinned npm package and the files the site needs from it."""

    package: str
    version: str
    licence: str
    output_dir: str  # relative to SITE_ROOT
    files: tuple[VendorFile, ...]


_THREE_IMPORT = (("three", "./three.module.js"),)

ASSETS: tuple[NpmAsset, ...] = (
    NpmAsset(
        package="three",
        version="0.187.0",
        licence="MIT",
        output_dir="vendor/three@0.187.0",
        files=(
            # Saved under their original names so the relative import between them holds.
            VendorFile("build/three.module.js", "three.module.js", minify=True),
            VendorFile("build/three.core.js", "three.core.js", minify=True),
            VendorFile(
                "examples/jsm/geometries/RoundedBoxGeometry.js",
                "RoundedBoxGeometry.js",
                rewrites=_THREE_IMPORT,
            ),
            VendorFile(
                "examples/jsm/utils/BufferGeometryUtils.js",
                "BufferGeometryUtils.js",
                rewrites=_THREE_IMPORT,
            ),
            VendorFile("LICENSE", "LICENSE"),
        ),
    ),
)

# First-party module roots whose import graph `check` walks.
MODULE_ROOTS = ("js/engine", "dev")

# Patterns that would break the production Content-Security-Policy.  They are searched
# in first-party files only; the vendored library is covered by the import graph check.
CSP_FORBIDDEN = (
    (r"\sstyle\s*=", "inline style attribute"),
    (r"<style\b", "style element"),
    (r"setAttribute\(\s*['\"]style['\"]", "setAttribute('style')"),
    (r"\.cssText\b", "cssText write"),
    (r"['\"(]data:", "data: URL"),
    (r"['\"(]blob:", "blob: URL"),
    (r"createObjectURL", "blob URL factory"),
    (r"\beval\(", "eval"),
    (r"new\s+Function\b", "Function constructor"),
    (r"new\s+(Shared)?Worker\b", "Worker"),
    (r"WebAssembly", "WebAssembly"),
    (r"addEventListener\(\s*['\"]scroll['\"]", "scroll listener"),
    (r"\bon(click|load|error)\s*=\s*['\"]", "inline event handler"),
    (r"https?://(?!127\.0\.0\.1|localhost)", "absolute URL"),
)
CSP_SCANNED = ("js/engine", "css/engine.css", "dev")
CSP_SUFFIXES = {".js", ".mjs", ".css", ".html"}

_STATEMENT_SPECIFIER = re.compile(
    r"""\b(?:import|export)\s*(?:[\w*${}\s,]+?\s*from\s*)?(?P<q>['"])(?P<spec>[^'"\n]+)(?P=q)"""
)
_DYNAMIC_SPECIFIER = re.compile(r"""\bimport\s*\(\s*(?P<q>['"])(?P<spec>[^'"\n]+)(?P=q)""")
# Any other relative .js path in a string: preload lists, `new URL(x, import.meta.url)`.
_PATH_LITERAL = re.compile(r"""(?P<q>['"])(?P<spec>\.{1,2}/[^'"\n]+\.js)(?P=q)""")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")


def _fetch(url: str, *, timeout: float = 180.0) -> bytes:
    if not url.startswith(f"{REGISTRY}/"):
        raise AssetError(f"refusing to fetch outside the npm registry: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "garage1886-vendor/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def _verify_integrity(blob: bytes, integrity: str) -> None:
    algorithm, _, expected = integrity.partition("-")
    if algorithm != "sha512" or not expected:
        raise AssetError(f"unsupported integrity value: {integrity!r}")
    actual = base64.b64encode(hashlib.sha512(blob).digest()).decode("ascii")
    if not hmac.compare_digest(actual, expected):
        raise AssetError("tarball does not match the registry integrity value")


def _read_members(tarball: bytes, wanted: Iterable[str]) -> dict[str, bytes]:
    """Read named files out of an npm tarball without extracting anything to disk."""
    remaining = set(wanted)
    found: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        for member in archive:
            name = member.name.removeprefix("package/")
            if name not in remaining or not member.isfile():
                continue
            handle = archive.extractfile(member)
            if handle is None:
                raise AssetError(f"cannot read {member.name} from the tarball")
            found[name] = handle.read()
            remaining.discard(name)
    if remaining:
        raise AssetError(f"missing from the tarball: {sorted(remaining)}")
    return found


def _minify(source: bytes, name: str, workdir: Path) -> bytes:
    """Minify one ES module with the pinned esbuild, keeping imports and licence comments."""
    source_dir = workdir / "source"
    output_dir = workdir / "minified"
    run_dir = workdir / "run"  # esbuild never runs from the directory holding the download
    for directory in (source_dir, output_dir, run_dir):
        directory.mkdir(exist_ok=True)
    source_path = source_dir / name
    output_path = output_dir / name
    source_path.write_bytes(source)
    command = [
        "npx",
        "--yes",
        ESBUILD,
        str(source_path),
        "--minify",
        "--format=esm",
        "--legal-comments=inline",
        "--log-level=warning",
        f"--outfile={output_path}",
    ]
    try:
        subprocess.run(command, cwd=run_dir, check=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as error:
        raise AssetError(f"esbuild failed for {name}: {error}") from error
    return output_path.read_bytes()


def _strip_comments(text: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def _specifiers(text: str, *, literals: bool = True) -> list[str]:
    """Module specifiers of static imports, re-exports and literal dynamic imports.

    With `literals`, also every other relative .js path that appears in a string.
    """
    bare = _strip_comments(text)
    found = [match["spec"] for match in _STATEMENT_SPECIFIER.finditer(bare)]
    found += [match["spec"] for match in _DYNAMIC_SPECIFIER.finditer(bare)]
    if literals:
        found += [match["spec"] for match in _PATH_LITERAL.finditer(bare)]
    return sorted(set(found))


def _rewrite_imports(source: bytes, rewrites: tuple[tuple[str, str], ...], name: str) -> bytes:
    text = source.decode("utf-8")
    for bare, relative in rewrites:
        pattern = re.compile(
            r"(\bfrom\s*)(['\"])" + re.escape(bare) + r"\2",
        )
        text, count = pattern.subn(lambda match: f"{match[1]}'{relative}'", text)
        if count == 0:
            raise AssetError(f"{name}: expected an import from {bare!r} to rewrite")
    leftovers = [spec for spec in _specifiers(text) if not spec.startswith(("./", "../"))]
    if leftovers:
        raise AssetError(f"{name}: bare imports remain after rewriting: {leftovers}")
    return text.encode("utf-8")


def _node_check(paths: Iterable[Path]) -> None:
    """Run `node --check` on ES modules (copied to .mjs so Node parses them as modules)."""
    with tempfile.TemporaryDirectory(prefix="site-node-check-") as scratch:
        for index, path in enumerate(paths):
            probe = Path(scratch) / f"module-{index}.mjs"
            shutil.copyfile(path, probe)
            result = subprocess.run(
                ["node", "--check", str(probe)],
                cwd=scratch,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if result.returncode != 0:
                raise AssetError(f"node --check failed for {path}:\n{result.stderr.strip()}")


def vendor_asset(asset: NpmAsset) -> Path:
    """Download, verify, transform and write one npm asset.  Returns its directory."""
    metadata = json.loads(_fetch(f"{REGISTRY}/{asset.package}/{asset.version}"))
    if metadata.get("version") != asset.version:
        raise AssetError(f"registry returned {metadata.get('version')} for {asset.package}")
    if metadata.get("license") != asset.licence:
        raise AssetError(
            f"{asset.package}@{asset.version} is licensed {metadata.get('license')!r}, "
            f"expected {asset.licence!r}"
        )
    tarball_url = metadata["dist"]["tarball"]
    integrity = metadata["dist"]["integrity"]
    print(f"{asset.package}@{asset.version}: downloading {tarball_url}")
    tarball = _fetch(tarball_url)
    _verify_integrity(tarball, integrity)
    print(f"  integrity ok ({integrity[:23]}..., {len(tarball):,} bytes)")

    members = _read_members(tarball, [item.member for item in asset.files])
    outputs: dict[str, bytes] = {}
    transforms: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="site-vendor-") as scratch:
        for item in asset.files:
            data = members[item.member]
            steps = []
            if item.rewrites:
                data = _rewrite_imports(data, item.rewrites, item.output)
                steps.append("rewrite bare imports to relative paths")
            if item.minify:
                print(f"  minifying {item.output} with {ESBUILD}")
                data = _minify(data, item.output, Path(scratch))
                steps.append(f"{ESBUILD} --minify --format=esm --legal-comments=inline")
            outputs[item.output] = data
            transforms[item.output] = "; ".join(steps) or "verbatim"

    scripts = {name for name in outputs if name.endswith(".js")}
    for name in sorted(scripts):
        for spec in _specifiers(outputs[name].decode("utf-8")):
            if not spec.startswith("./") or spec[2:] not in scripts:
                raise AssetError(f"{name}: import {spec!r} leaves the vendored directory")

    target = SITE_ROOT / asset.output_dir
    target.mkdir(parents=True, exist_ok=True)
    manifest_files = []
    for item in asset.files:
        data = outputs[item.output]
        (target / item.output).write_bytes(data)
        manifest_files.append(
            {
                "path": item.output,
                "from": f"package/{item.member}",
                "transform": transforms[item.output],
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        print(f"  wrote {asset.output_dir}/{item.output} ({len(data):,} bytes)")
    manifest = {
        "package": asset.package,
        "version": asset.version,
        "licence": asset.licence,
        "source": tarball_url,
        "integrity": integrity,
        "generated_by": "scripts/vendor_site_assets.py vendor",
        "files": manifest_files,
    }
    (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    _node_check(target / name for name in sorted(scripts))
    print(f"  node --check ok ({len(scripts)} modules)")
    return target


# --------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------


def _check_manifests() -> list[str]:
    problems: list[str] = []
    for asset in ASSETS:
        directory = SITE_ROOT / asset.output_dir
        manifest_path = directory / MANIFEST_NAME
        if not manifest_path.is_file():
            problems.append(f"{asset.output_dir}: missing {MANIFEST_NAME} (run `vendor`)")
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("version") != asset.version:
            problems.append(f"{asset.output_dir}: manifest is for {manifest.get('version')}")
        listed = {entry["path"] for entry in manifest["files"]}
        on_disk = {path.name for path in directory.iterdir()} - {MANIFEST_NAME}
        if listed != on_disk:
            problems.append(f"{asset.output_dir}: files differ from manifest: {listed ^ on_disk}")
        for entry in manifest["files"]:
            path = directory / entry["path"]
            if not path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != entry["sha256"]:
                problems.append(f"{asset.output_dir}/{entry['path']}: sha256 mismatch")
    return problems


def _site_modules() -> list[Path]:
    modules: list[Path] = []
    for asset in ASSETS:
        modules += sorted((SITE_ROOT / asset.output_dir).glob("*.js"))
    for root in MODULE_ROOTS:
        modules += sorted((SITE_ROOT / root).rglob("*.js"))
    return modules


def _check_import_graph(modules: list[Path]) -> list[str]:
    """Every import is relative, resolves to a real file and gives each module one URL."""
    problems: list[str] = []
    urls_by_file: dict[Path, set[str]] = {}
    vendor_dirs = [SITE_ROOT / asset.output_dir for asset in ASSETS]
    for module in modules:
        if module.is_symlink():
            problems.append(f"{module.relative_to(SITE_ROOT)}: symlinked module")
        source = module.read_text(encoding="utf-8")
        imported = set(_specifiers(source, literals=False))
        for spec in _specifiers(source):
            where = f"{module.relative_to(SITE_ROOT)} refers to {spec!r}"
            if not spec.startswith(("./", "../")):
                problems.append(f"{where}: not a relative specifier")
                continue
            if "?" in spec or "#" in spec:
                problems.append(f"{where}: query or fragment makes a second module URL")
                continue
            joined = PurePosixPath(module.parent.relative_to(SITE_ROOT).as_posix()) / spec
            url = os.path.normpath(joined.as_posix())
            target = SITE_ROOT / url
            if url.startswith("..") or not target.is_file():
                problems.append(f"{where}: does not resolve inside the site")
                continue
            urls_by_file.setdefault(target.resolve(), set()).add(url)
            inside_vendor = any(target.parent == directory for directory in vendor_dirs)
            from_vendor = any(module.parent == directory for directory in vendor_dirs)
            core = inside_vendor and target.name.endswith(".core.js")
            if core and spec in imported and not from_vendor:
                problems.append(f"{where}: import the package entry point, not its core file")
    for target, urls in urls_by_file.items():
        if len(urls) > 1:
            problems.append(f"{target}: reachable under several URLs {sorted(urls)}")
    lowered: dict[str, Path] = {}
    for module in modules:
        key = module.relative_to(SITE_ROOT).as_posix().lower()
        if key in lowered:
            problems.append(f"{module} and {lowered[key]} differ only by case")
        lowered[key] = module
    vendored_hashes = {
        hashlib.sha256(path.read_bytes()).hexdigest(): path
        for directory in vendor_dirs
        for path in directory.glob("*.js")
    }
    for path in SITE_ROOT.rglob("*.js"):
        if any(path.parent == directory for directory in vendor_dirs):
            continue
        twin = vendored_hashes.get(hashlib.sha256(path.read_bytes()).hexdigest())
        if twin is not None:
            problems.append(f"{path}: second copy of vendored module {twin.name}")
    return problems


def _check_csp() -> list[str]:
    problems: list[str] = []
    files: list[Path] = []
    for entry in CSP_SCANNED:
        path = SITE_ROOT / entry
        if path.is_dir():
            files += [item for item in sorted(path.rglob("*")) if item.suffix in CSP_SUFFIXES]
        elif path.is_file():
            files.append(path)
    for path in files:
        text = path.read_text(encoding="utf-8")
        if path.suffix != ".html":
            text = _strip_comments(text)
        for pattern, label in CSP_FORBIDDEN:
            for match in re.finditer(pattern, text):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{path.relative_to(SITE_ROOT)}:{line}: {label}")
    return problems


def run_checks() -> int:
    modules = _site_modules()
    problems = _check_manifests() + _check_import_graph(modules) + _check_csp()
    try:
        _node_check(modules)
    except AssetError as error:
        problems.append(str(error))
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        return 1
    print(f"ok: {len(modules)} modules, manifests, import graph, CSP patterns, node --check")
    return 0


# --------------------------------------------------------------------------------------
# Headless Chrome over the DevTools pipe (no WebSocket client needed)
# --------------------------------------------------------------------------------------


class Chrome:
    """A headless Chrome driven through `--remote-debugging-pipe`.

    Chrome reads NUL-terminated JSON commands on file descriptor 3 and writes
    replies and events on file descriptor 4.
    """

    def __init__(self, binary: str = "google-chrome", *, extra_args: Iterable[str] = ()):
        self._profile = tempfile.mkdtemp(prefix="site-chrome-")
        command_read, command_write = os.pipe()
        event_read, event_write = os.pipe()
        # Move the child's ends out of the way of the 3 and 4 the shell assigns below.
        child_in = fcntl.fcntl(command_read, fcntl.F_DUPFD, 20)
        child_out = fcntl.fcntl(event_write, fcntl.F_DUPFD, 20)
        os.close(command_read)
        os.close(event_write)
        arguments = [
            "--headless=new",
            "--remote-debugging-pipe",
            f"--user-data-dir={self._profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--hide-scrollbars",
            "--mute-audio",
            "--force-color-profile=srgb",
            # Software WebGL: slow, but deterministic and independent of the host GPU.
            "--use-gl=angle",
            "--use-angle=swiftshader-webgl",
            "--enable-unsafe-swiftshader",
            "--ignore-gpu-blocklist",
            *extra_args,
        ]
        self._process = subprocess.Popen(
            ["/bin/sh", "-c", f'exec "$0" "$@" 3<&{child_in} 4>&{child_out}', binary, *arguments],
            pass_fds=(child_in, child_out),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(child_in)
        os.close(child_out)
        self._write_fd = command_write
        self._read_fd = event_read
        self._buffer = b""
        self._next_id = 0
        self.events: list[dict] = []

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.call("Browser.close", timeout=5)
        for descriptor in (self._write_fd, self._read_fd):
            with contextlib.suppress(OSError):
                os.close(descriptor)
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=10)
        shutil.rmtree(self._profile, ignore_errors=True)

    def __enter__(self) -> Chrome:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _read_message(self, deadline: float) -> dict:
        while b"\0" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssetError("timed out waiting for Chrome")
            ready, _, _ = select.select([self._read_fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self._read_fd, 1 << 20)
            if not chunk:
                raise AssetError("Chrome closed the DevTools pipe")
            self._buffer += chunk
        raw, _, self._buffer = self._buffer.partition(b"\0")
        return json.loads(raw)

    def call(
        self,
        method: str,
        params: dict | None = None,
        *,
        session: str | None = None,
        timeout: float = 60.0,
    ) -> dict:
        self._next_id += 1
        message: dict = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        os.write(self._write_fd, json.dumps(message).encode("utf-8") + b"\0")
        deadline = time.monotonic() + timeout
        while True:
            reply = self._read_message(deadline)
            if reply.get("id") == self._next_id:
                if "error" in reply:
                    raise AssetError(f"{method}: {reply['error'].get('message')}")
                return reply.get("result", {})
            if "method" in reply:
                self.events.append(reply)

    def pump(self, seconds: float) -> None:
        """Collect events for a while without sending anything."""
        deadline = time.monotonic() + seconds
        with contextlib.suppress(AssetError):
            while True:
                reply = self._read_message(deadline)
                if "method" in reply:
                    self.events.append(reply)


# Chrome's own remark about how a screenshot reads the software-rendered canvas back.
BENIGN = ("GPU stall due to ReadPixels",)


class Page:
    """One tab with a fixed viewport, console capture and a transparent backdrop."""

    def __init__(
        self,
        chrome: Chrome,
        *,
        width: int,
        height: int,
        scale: float = 1.0,
        mobile: bool = False,
        reduced_motion: bool = False,
        transparent: bool = False,
    ):
        self.chrome = chrome
        target = chrome.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.target = target
        self.session = chrome.call(
            "Target.attachToTarget", {"targetId": target, "flatten": True}
        )["sessionId"]
        for domain in ("Page", "Runtime", "Log"):
            self.send(f"{domain}.enable")
        self.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": scale, "mobile": mobile},
        )
        if mobile:
            self.send("Emulation.setTouchEmulationEnabled", {"enabled": True})
        if transparent:
            self.send(
                "Emulation.setDefaultBackgroundColorOverride",
                {"color": {"r": 0, "g": 0, "b": 0, "a": 0}},
            )
        if reduced_motion:
            self.send(
                "Emulation.setEmulatedMedia",
                {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]},
            )

    def send(self, method: str, params: dict | None = None, *, timeout: float = 60.0) -> dict:
        return self.chrome.call(method, params, session=self.session, timeout=timeout)

    def navigate(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})

    def evaluate(self, expression: str, *, timeout: float = 60.0):
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            raise AssetError(f"page script failed: {result['exceptionDetails'].get('text')}")
        return result["result"].get("value")

    def wait_for(self, expression: str, *, timeout: float = 120.0, interval: float = 0.1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.evaluate(expression)
            if value:
                return value
            self.chrome.pump(interval)
        raise AssetError(f"timed out waiting for: {expression}")

    def screenshot(self, path: Path) -> None:
        data = self.send("Page.captureScreenshot", {"format": "png"}, timeout=180)["data"]
        path.write_bytes(base64.b64decode(data))

    def console_problems(self) -> list[str]:
        """Console errors, warnings, uncaught exceptions and CSP reports seen so far."""
        return [text for text in self._console() if not any(mark in text for mark in BENIGN)]

    def _console(self) -> list[str]:
        problems: list[str] = []
        for event in self.chrome.events:
            if event.get("sessionId") != self.session:
                continue
            method, params = event["method"], event.get("params", {})
            if method == "Runtime.exceptionThrown":
                details = params["exceptionDetails"]
                text = details.get("exception", {}).get("description") or details.get("text")
                problems.append(f"exception: {text}")
            elif method == "Runtime.consoleAPICalled" and params["type"] in ("error", "warning"):
                text = " ".join(
                    str(arg.get("value", arg.get("description", ""))) for arg in params["args"]
                )
                problems.append(f"console.{params['type']}: {text}")
            elif method == "Log.entryAdded" and params["entry"]["level"] in ("error", "warning"):
                entry = params["entry"]
                problems.append(f"log.{entry['level']}: {entry['text']} {entry.get('url', '')}")
        return problems

    def close(self) -> None:
        with contextlib.suppress(AssetError):
            self.chrome.call("Target.closeTarget", {"targetId": self.target}, timeout=10)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".webp": "image/webp",
    }

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


@contextlib.contextmanager
def serve_site(root: Path = SITE_ROOT) -> Iterator[str]:
    """Serve the static site on a free loopback port for the duration of the block."""
    handler = functools.partial(_QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _magick(*arguments: str | Path) -> None:
    try:
        subprocess.run(["magick", *map(str, arguments)], check=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as error:
        raise AssetError(f"magick failed: {error}") from error


# --------------------------------------------------------------------------------------
# Posters and baked pin positions
# --------------------------------------------------------------------------------------

POSTER_SIZES = {"portrait": (800, 1200), "landscape": (1600, 1000)}
POSTER_PROGRESS = {0: 0.0, 1: 0.8}
POSTER_SUPERSAMPLE = 2
ENGINE_IMAGES = SITE_ROOT / "img" / "engine"


def render_posters(chrome_binary: str, *, quality: int, keep_png: Path | None) -> None:
    """Render the four posters from the dev harness and bake the pin positions."""
    ENGINE_IMAGES.mkdir(parents=True, exist_ok=True)
    pins: dict[str, object] = {"version": 1, "progress": POSTER_PROGRESS[1]}
    pins_path = ENGINE_IMAGES / "pins.json"
    if not pins_path.exists():
        pins_path.write_text("{}\n", encoding="utf-8")  # the harness fetches it
    with (
        tempfile.TemporaryDirectory(prefix="site-posters-") as scratch,
        serve_site() as origin,
        Chrome(chrome_binary) as chrome,
    ):
        for layout, (width, height) in POSTER_SIZES.items():
            for index, progress in POSTER_PROGRESS.items():
                page = Page(
                    chrome,
                    width=width,
                    height=height,
                    scale=POSTER_SUPERSAMPLE,
                    transparent=True,
                )
                url = (
                    f"{origin}/dev/engine.html?layout={layout}&p={progress}"
                    "&engine=3d&poster=1"
                )
                page.navigate(url)
                page.wait_for("document.documentElement.dataset.engineReady === '1'", timeout=300)
                # The posters being rendered may not exist yet; anything else is a bug.
                problems = [
                    text for text in page.console_problems() if "/img/engine/poster-" not in text
                ]
                if problems:
                    raise AssetError(f"{url} reported: {problems}")
                png = Path(scratch) / f"poster-{layout}-{index}.png"
                page.screenshot(png)
                if keep_png is not None:
                    keep_png.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(png, keep_png / png.name)
                target = ENGINE_IMAGES / f"poster-{layout}-{index}.webp"
                _magick(
                    png,
                    "-filter",
                    "Lanczos",
                    "-resize",
                    f"{width}x{height}!",
                    "-define",
                    "webp:method=6",
                    "-define",
                    "webp:alpha-quality=90",
                    "-define",
                    "webp:alpha-filtering=2",
                    "-quality",
                    str(quality),
                    target,
                )
                print(f"wrote {target.relative_to(REPO_ROOT)} ({target.stat().st_size:,} bytes)")
                if index == 1:
                    baked = page.evaluate("document.documentElement.dataset.engineBaked || ''")
                    if not baked:
                        raise AssetError(f"{url} did not publish pin positions")
                    pins[layout] = json.loads(baked)
                page.close()
    pins_path.write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {pins_path.relative_to(REPO_ROOT)}")


# --------------------------------------------------------------------------------------
# Spray starburst
# --------------------------------------------------------------------------------------

BURST_SIZE = 1280
BURST_SEED = 1886


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    if edge0 == edge1:
        return 0.0 if value < edge0 else 1.0
    t = (value - edge0) / (edge1 - edge0)
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    return t * t * (3.0 - 2.0 * t)


def _ray_profile(rng: random.Random, bins: int, count: int, core: float, spread: float):
    """Reach of the paint (fraction of the half size) for every angular bin."""
    reach = [core] * bins
    for _ in range(count):
        centre = rng.random() * bins
        length = core + (1.0 - core) * (0.35 + 0.65 * rng.random() ** 0.8)
        half_width = bins * spread * (0.35 + rng.random() ** 2 * 1.4) / (2 * math.pi)
        sharpness = 1.3 + rng.random() * 1.4
        for offset in range(-int(half_width) - 1, int(half_width) + 2):
            falloff = 1.0 - abs(offset) / half_width
            if falloff <= 0.0:
                continue
            index = int(centre + offset) % bins
            value = core + (length - core) * falloff**sharpness
            if value > reach[index]:
                reach[index] = value
    # Wobble the core edge so it is not a perfect circle.
    wobble = [rng.random() for _ in range(24)]
    for index in range(bins):
        position = index / bins * len(wobble)
        low = int(position) % len(wobble)
        high = (low + 1) % len(wobble)
        mix = position - int(position)
        mix = mix * mix * (3 - 2 * mix)
        noise = wobble[low] * (1 - mix) + wobble[high] * mix
        reach[index] *= 0.94 + 0.12 * noise
    return reach


def _spray_mask(
    size: int,
    seed: int,
    *,
    rays: int,
    core: float,
    spread: float,
    softness: float,
    speckle: float,
    droplets: int,
    start: float = 0.0,
) -> bytearray:
    """One spray-can layer as an 8-bit coverage mask.

    The soft edge is stippled rather than blurred: real spray paint thins out into
    separate dots, it does not fade like a gradient.
    """
    rng = random.Random(seed)
    bins = 4096
    reach = _ray_profile(rng, bins, rays, core, spread)
    half = size / 2
    mask = bytearray(size * size)
    two_pi = 2 * math.pi
    atan2, sqrt, rand = math.atan2, math.sqrt, rng.random
    for y in range(size):
        dy = (y + 0.5 - half) / half
        row = y * size
        for x in range(size):
            dx = (x + 0.5 - half) / half
            radius = sqrt(dx * dx + dy * dy)
            if radius > 1.02:
                continue
            angle = atan2(dy, dx)
            limit = reach[int((angle + math.pi) / two_pi * bins) % bins]
            soft = softness * (0.6 + 0.8 * limit)
            coverage = 1.0 - _smoothstep(limit - soft, limit + soft * 0.5, radius)
            if start > 0.0:
                coverage *= _smoothstep(start * 0.6, start * 1.25, radius)
            # Overspray: a thin dust of dots well past the edge of each ray.
            dust = speckle * (1.0 - _smoothstep(limit * 0.9, min(1.0, limit + 0.3), radius))
            if coverage <= 0.004 and dust <= 0.001:
                continue
            if 0.02 < coverage < 0.98:
                grain = 1.0 if rand() < coverage else 0.0
                coverage = coverage * 0.35 + grain * 0.65
            elif coverage <= 0.02 and rand() < dust:
                coverage = 0.55 + 0.45 * rand()
            mask[row + x] = int(max(0.0, min(1.0, coverage)) * 255)
    # Fat droplets thrown off along the rays.
    for _ in range(droplets):
        index = int(rand() * bins)
        limit = reach[index]
        angle = index / bins * two_pi - math.pi
        distance = limit * (0.82 + rand() * 0.42)
        if distance > 0.99 or distance < start:
            continue
        cx = half + math.cos(angle) * distance * half
        cy = half + math.sin(angle) * distance * half
        dot = 0.7 + rand() ** 3 * size * 0.0042
        span = int(dot) + 2
        for py in range(int(cy) - span, int(cy) + span + 1):
            if not 0 <= py < size:
                continue
            for px in range(int(cx) - span, int(cx) + span + 1):
                if not 0 <= px < size:
                    continue
                gap = sqrt((px + 0.5 - cx) ** 2 + (py + 0.5 - cy) ** 2)
                value = int(255 * (1.0 - _smoothstep(dot - 0.8, dot + 0.4, gap)))
                if value > mask[py * size + px]:
                    mask[py * size + px] = value
    return mask


def _haze_mask(size: int, seed: int, within: bytearray) -> bytearray:
    """Dry white overspray drifting across the black: a ring of soft stipple.

    Solid black would swallow the dark parts of the engine; a mottled charcoal
    field behind them lets a black block or belt read as a shape.
    """
    rng = random.Random(seed)
    cells = 14
    lattice = [[rng.random() for _ in range(cells + 1)] for _ in range(cells + 1)]
    half = size / 2
    mask = bytearray(size * size)
    rand, sqrt, exp = rng.random, math.sqrt, math.exp
    # Paint in 2 px drops: a spray can does not resolve single pixels.
    for y in range(0, size - 1, 2):
        dy = (y + 1 - half) / half
        gy = (y / size) * cells
        y0 = int(gy)
        fy = gy - y0
        fy = fy * fy * (3 - 2 * fy)
        row = y * size
        for x in range(0, size - 1, 2):
            inside = within[row + x]
            if inside < 40:
                continue
            dx = (x + 1 - half) / half
            radius = sqrt(dx * dx + dy * dy)
            if radius > 0.78:
                continue
            gx = (x / size) * cells
            x0 = int(gx)
            fx = gx - x0
            fx = fx * fx * (3 - 2 * fx)
            top = lattice[y0][x0] * (1 - fx) + lattice[y0][x0 + 1] * fx
            bottom = lattice[y0 + 1][x0] * (1 - fx) + lattice[y0 + 1][x0 + 1] * fx
            cloud = top * (1 - fy) + bottom * fy
            ring = exp(-(((radius - 0.3) / 0.21) ** 2))
            density = 0.5 * ring * (0.12 + 1.6 * cloud * cloud * cloud) * (inside / 255)
            if rand() < density:
                value = int(120 + 135 * rand())
                mask[row + x] = value
                mask[row + x + 1] = value
                mask[row + size + x] = value
                mask[row + size + x + 1] = value
    return mask


def _write_pgm(path: Path, size: int, mask: bytearray) -> None:
    path.write_bytes(f"P5 {size} {size} 255\n".encode("ascii") + bytes(mask))


def render_burst(*, quality: int) -> None:
    """Paint the black spray starburst (with dry white strokes) behind the engine."""
    ENGINE_IMAGES.mkdir(parents=True, exist_ok=True)
    size = BURST_SIZE
    with tempfile.TemporaryDirectory(prefix="site-burst-") as scratch:
        work = Path(scratch)
        layers = {
            "black": _spray_mask(
                size, BURST_SEED, rays=52, core=0.5, spread=0.2,
                softness=0.065, speckle=0.06, droplets=620,
            ),
            "white": _spray_mask(
                size, BURST_SEED + 7, rays=15, core=0.12, spread=0.075,
                softness=0.03, speckle=0.01, droplets=110, start=0.5,
            ),
            "gold": _spray_mask(
                size, BURST_SEED + 13, rays=6, core=0.1, spread=0.04,
                softness=0.018, speckle=0.004, droplets=40, start=0.58,
            ),
        }
        layers["haze"] = _haze_mask(size, BURST_SEED + 29, layers["black"])
        for name, mask in layers.items():
            _write_pgm(work / f"{name}.pgm", size, mask)
        target = ENGINE_IMAGES / "burst.webp"

        def layer(name: str, colour: str, blur: str, opacity: str) -> list[str]:
            return [
                "(", "-size", f"{size}x{size}", f"xc:{colour}",
                "(", str(work / f"{name}.pgm"), "-blur", blur, "-evaluate", "multiply", opacity,
                ")",
                "-alpha", "off", "-compose", "CopyOpacity", "-composite", ")",
            ]

        _magick(
            *layer("black", "#0C0C0D", "0x1.2", "1.0"),
            # A wide soft mist under the hard paint, as the can leaves around every stroke.
            *layer("black", "#0C0C0D", "0x24", "0.55"),
            "-compose", "DstOver", "-composite",
            *layer("haze", "#D9D6CF", "0x1.3", "0.56"),
            "-compose", "Over", "-composite",
            *layer("white", "#F2F0EC", "0x1.0", "0.72"),
            "-compose", "Over", "-composite",
            *layer("gold", "#FFBC00", "0x0.9", "0.8"),
            "-compose", "Over", "-composite",
            "-define", "webp:method=6",
            # The bytes are in the stippled alpha channel. Much below 50 the soft mist bands.
            "-define", "webp:alpha-quality=50",
            "-define", "webp:alpha-filtering=2",
            "-quality", str(quality),
            target,
        )
        print(f"wrote {target.relative_to(REPO_ROOT)} ({target.stat().st_size:,} bytes)")


# --------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("vendor", help="download, verify and minify third-party code")
    commands.add_parser("check", help="verify vendored files, the import graph and CSP rules")
    burst = commands.add_parser("burst", help="regenerate img/engine/burst.webp")
    burst.add_argument("--quality", type=int, default=72)
    posters = commands.add_parser("posters", help="render engine posters and pins.json")
    posters.add_argument("--chrome", default="google-chrome")
    posters.add_argument("--quality", type=int, default=84)
    posters.add_argument("--keep-png", type=Path, default=None, help="also copy the raw PNGs here")
    options = parser.parse_args(argv)
    try:
        if options.command == "vendor":
            for asset in ASSETS:
                vendor_asset(asset)
            return run_checks()
        if options.command == "check":
            return run_checks()
        if options.command == "burst":
            render_burst(quality=options.quality)
            return 0
        render_posters(options.chrome, quality=options.quality, keep_png=options.keep_png)
        return 0
    except AssetError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
