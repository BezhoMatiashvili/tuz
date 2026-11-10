#!/usr/bin/env python3
"""Build the brand assets of the public landing page from the owner's files.

Everything the landing page shows that is not code comes out of this script: the
fonts, the icon sprite, the logo SVGs and favicons, the wall art cut from the shop's
own mural photos, the gallery photos and the Open Graph image.  It is run by hand at
authoring time.  The site has no build step and nothing here is needed to serve it.

    python3 -I scripts/build_site_assets.py all       # every step below, in this order
    python3 -I scripts/build_site_assets.py fonts     # fonts/*.woff2, licences, FONTS.json
    python3 -I scripts/build_site_assets.py icons     # vendor/phosphor/sprite.svg
    python3 -I scripts/build_site_assets.py logo      # img/logo-full.svg, wordmark.svg, favicons
    python3 -I scripts/build_site_assets.py art       # burst masks, lettering, sheen, grain
    python3 -I scripts/build_site_assets.py photos    # service and story murals, gallery, og.jpg
    python3 -I scripts/build_site_assets.py check     # recorded SHA-256 still match the files

Only the Python standard library is used.  External tools: ImageMagick 7 (`magick`,
with WebP), poppler (`pdftocairo`) and `uvx`, which runs the pinned fonttools.  Every
input is pinned by SHA-256: the owner's brand files (``--brand DIR``, or the
``GARAGE_BRAND_DIR`` environment variable) and the three downloads below.

The engine section's assets belong to ``scripts/vendor_site_assets.py``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_ROOT = REPO_ROOT / "automotive" / "static" / "site"
IMAGES = SITE_ROOT / "img"
FONTS = SITE_ROOT / "fonts"
ICONS = SITE_ROOT / "vendor" / "phosphor"
GENERATOR = "scripts/build_site_assets.py"
DEFAULT_BRAND_DIR = "~/Downloads/WhatsApp Unknown 2026-11-09 at 23.44.02"
# A fixed clock keeps the font files byte-identical between runs.
SOURCE_DATE_EPOCH = "1794268800"

# --------------------------------------------------------------------------------------
# Pinned inputs
# --------------------------------------------------------------------------------------

_WALL = "WhatsApp Image 2026-11-07 at 12.04.30"
_WALL_2 = "WhatsApp Image 2026-11-10 at"
# The owner's files: the vector logo and twelve photos of the shop's painted walls.
BRAND_FILES: dict[str, tuple[str, str]] = {
    "logo": ("garage.pdf",
             "0c7b590377e21b3752114b31e761165b4229d2d736252542e0cd9aec68066bc1"),
    "turbo": (f"{_WALL}.jpeg",
              "a3b85ff858baef281141c657f008bac39499ba8a6179a7f68263f79d6e1a1237"),
    "route66": (f"{_WALL} (1).jpeg",
                "46d949a05e3132b969c7b645e63be0ed2e71443012162b4f78637d60ab17b84e"),
    "engine": (f"{_WALL} (2).jpeg",
               "ce46fc5412a32dd6f01fbe6eaa8959d13ef78245063c9b9747b591b1f4e409cf"),
    "brakes": (f"{_WALL} (3).jpeg",
               "0f2995f29a89b085600975c3f8e175c7ee55397dafe711ab64849725265e454b"),
    "logo_wall": (f"{_WALL} (4).jpeg",
                  "7327ae8c085a179b31406338d40072c7fdf95c636d5874e7265adc4ecc486bdc"),
    "service": (f"{_WALL} (5).jpeg",
                "7e34cddc58761dd5aa83624fd05a9603bd7b6125e89460e212da95f7cad05d3c"),
    "lubricants": (f"{_WALL} (6).jpeg",
                   "7ed677e10709fe11c557feea2bbf362b1fa0f1581fbe46e5cab4ba97c26648e4"),
    # Sent three days later: the wall that tells where the name comes from, and second
    # shots of three walls above.
    "benz": (f"{_WALL_2} 14.20.03.jpeg",
             "00c9dc4fea3c7a2756d222b054845320f2eb8e100dbb4e0f7db8e6073ef58248"),
    "horse": (f"{_WALL_2} 14.20.04.jpeg",
              "3ac6ee7b523acf07811b70dc2d2c6b48da353d06ff2f6d9741327412f07542ef"),
    "horse_wide": (f"{_WALL_2} 14.20.04 (1).jpeg",
                   "779e0704285828aabb1d782dfbfd0b63846af4f5e9fd24f7cdda1208770a29b7"),
    "engine_close": (f"{_WALL_2} 14.20.04 (2).jpeg",
                     "51bd6fa4301b60e32ace4d9f7bde54d0008ce85a61ab61aeaa42bebf6cdb6a56"),
    "lubricants_clear": (f"{_WALL_2} 14.20.04 (3).jpeg",
                         "7a0127a7d977f1e213df11585801a4bb24750f3c688ccfc2dfc21d0526b845ec"),
}

GOOGLE_FONTS_COMMIT = "8b0a1d0f5983c89bc2b93f1b5fb55f9e252744b5"
_GF = f"https://raw.githubusercontent.com/google/fonts/{GOOGLE_FONTS_COMMIT}/ofl"
DOWNLOADS: dict[str, tuple[str, str]] = {
    "noto": (f"{_GF}/notosansgeorgian/NotoSansGeorgian%5Bwdth%2Cwght%5D.ttf",
             "dc591156f36842d38996c4a7a17fee9bb58e45da3e2cac7a31b7d33de700adb9"),
    "noto_ofl": (f"{_GF}/notosansgeorgian/OFL.txt",
                 "8c02263c5d73d40544f9ed91e30c4e947407057a3cc430d7b786189aeceff6df"),
    "barlow": (f"{_GF}/barlowcondensed/BarlowCondensed-ExtraBoldItalic.ttf",
               "331783dae9ec0c398f4d32c9fd5a56d4c602caa0c1628c2b9cbfe93f48eb68c9"),
    "barlow_ofl": (f"{_GF}/barlowcondensed/OFL.txt",
                   "186d750eb496a4c17a76385f82be6aea2ac1cf2de074a811d63786cf374ea73f"),
    "phosphor": ("https://registry.npmjs.org/@phosphor-icons/core/-/core-2.1.1.tgz",
                 "313332be6190b724da24107addd781799b48bf76b13963f24501112ffe1baadd"),
}
PHOSPHOR_VERSION = "2.1.1"
PHOSPHOR_INTEGRITY = ("sha512-v4ARvrip4qBCImOE5rmPUylOEK4iiED9ZyKjcvzuezqMaiRASCHKcRIuvvxL/"
                      "twvLpkfnEODCOJp5dM4eZilxQ==")
FONTTOOLS = "fonttools[woff]==4.60.1"

# Basic Latin, the punctuation the copy uses and the Lari sign.
LATIN = ("U+0020-007E,U+00A0,U+00A9,U+00AB,U+00B7,U+00BB,U+00D7,U+2010,U+2013,U+2014,"
         "U+2018-201A,U+201C-201E,U+2022,U+2026")
# Mkhedruli and the Mtavruli capitals `text-transform: uppercase` maps it to.
GEORGIAN = "U+10D0-10FF,U+1C90-1CBA,U+1CBD-1CBF,U+20BE,U+2116"
FONT_FILES: tuple[dict, ...] = (
    {"output": "noto-sans-georgian-400.woff2", "source": "noto",
     "axes": {"wght": 400, "wdth": 100}, "unicodes": f"{LATIN},{GEORGIAN}",
     "features": "kern,mark,mkmk,ccmp,locl,case"},
    {"output": "noto-sans-georgian-600.woff2", "source": "noto",
     "axes": {"wght": 600, "wdth": 100}, "unicodes": f"{LATIN},{GEORGIAN}",
     "features": "kern,mark,mkmk,ccmp,locl,case"},
    # The display cut. `capitals` points the Mkhedruli code points at the Mtavruli glyphs.
    {"output": "noto-sans-georgian-xcond-800.woff2", "source": "noto",
     "axes": {"wght": 800, "wdth": 62.5}, "unicodes": f"{LATIN},{GEORGIAN}",
     "features": "kern,mark,mkmk,ccmp,locl,case", "capitals": True},
    {"output": "barlow-condensed-800i.woff2", "source": "barlow",
     "axes": {}, "unicodes": LATIN, "features": "kern,mark,mkmk,ccmp,locl,liga"},
)

# Georgian headlines are set in Mtavruli capitals while the page text stays lowercase.
# CSS cannot be relied on for that: Chrome leaves Georgian alone under
# `text-transform: uppercase` (Firefox maps it only when the font has the capitals). So
# the display cut itself draws a capital for every Mkhedruli letter; the Mtavruli code
# points keep their own glyphs, so an engine that does map the text gets the same result.
# The copy is renamed, as the OFL asks of a modified version. Run with the pinned fonttools.
CAPITALS_SCRIPT = """
import sys
from fontTools.ttLib import TTFont

font = TTFont(sys.argv[1])
pairs = [(0x10D0 + i, 0x1C90 + i) for i in range(0x2B)]
pairs += [(0x10FD + i, 0x1CBD + i) for i in range(3)]
for table in font["cmap"].tables:
    if not table.isUnicode():
        continue
    for lower, upper in pairs:
        if upper in table.cmap:
            table.cmap[lower] = table.cmap[upper]
for record in font["name"].names:
    if record.nameID in (1, 3, 4, 6, 16):
        text = record.toUnicode()
        if record.nameID == 6:
            text = text.replace("NotoSansGeorgian", "G1886GeorgianCapitals")
        else:
            text = text.replace("Noto Sans Georgian", "G1886 Georgian Capitals")
        record.string = text
font.save(sys.argv[2])
"""

# Service icons the dashboard can store (automotive.site_content.SERVICE_ICONS), then
# the interface glyphs the page uses.
ICON_NAMES: tuple[str, ...] = (
    "wrench", "gauge", "drop", "disc", "steering-wheel", "engine", "flag-checkered",
    "package", "tire", "car-battery", "fan", "snowflake", "lightning", "headlights",
    "toolbox", "car-profile", "gas-pump", "thermometer", "shield-check", "sparkle",
    "check", "phone", "map-pin", "clock", "list", "x", "chat", "chat-text",
    "magnifying-glass", "caret-down", "caret-left", "caret-right", "arrow-right",
    "arrow-up-right", "arrow-clockwise", "plus", "warning", "info", "envelope-simple",
    "paper-plane-right", "whatsapp-logo", "facebook-logo", "instagram-logo", "tiktok-logo",
)

# Icons that CSS uses as a background image cannot take `currentColor`, and the policy
# allows no data: URL, so each is also written as a file of its own in a fixed colour.
STANDALONE_ICONS: dict[str, str] = {"caret-down": "#C9C7C2"}  # select chevron, --paper-300

# Gallery photos in page order.  Only walls that carry no other company's marks are
# published; the owner can publish the rest from the dashboard once that is cleared.
# The 1886 wall shows a man and a year, not a mark, so it is published.  A photo is only
# ever added at the end of what is already stored (`site_setup --add-photos`), so the
# order here is the order of a first setup.
GALLERY: tuple[dict, ...] = (
    {"source": "logo_wall", "file": "logo-wall.webp", "published": True,
     "alt_ka": "Garage 1886-ის ოქროსფერი ლოგო შავ ფონზე, წითელ კედელზე, წარწერით „ფარეხი“",
     "alt_en": "The gold Garage 1886 logo painted on a black burst on our red wall"},
    {"source": "service", "file": "service-wall.webp", "published": True,
     "alt_ka": "კედლის მხატვრობა წარწერით Service და ავტონაწილებით",
     "alt_en": "The Service mural with hand-painted car parts"},
    {"source": "benz", "file": "benz-1886-wall.webp", "published": True,
     "alt_ka": "კედლის მხატვრობა: 1886 გვირგვინით, კარლ ბენცის პორტრეტი და პირველი "
               "ავტომობილის ისტორია",
     "alt_en": "Mural of the year 1886 under a crown, a portrait of Karl Benz and the story "
               "of the first automobile"},
    {"source": "brakes", "file": "brakes-wall.webp", "published": False,
     "alt_ka": "კედლის მხატვრობა: სამუხრუჭე დისკი, სუპორტი და წარწერა More than a Garage",
     "alt_en": "Mural of a brake disc and caliper under the words More than a Garage"},
    {"source": "engine", "file": "engine-wall.webp", "published": False,
     "alt_ka": "კედლის მხატვრობა: V12 ძრავა და წარწერები Build, Service, Diagnostics, Performance",
     "alt_en": "Mural of a V12 engine with the words Build, Service, Diagnostics, Performance"},
    {"source": "turbo", "file": "turbo-wall.webp", "published": False,
     "alt_ka": "კედლის მხატვრობა: ტურბინა, დგუში და ამორტიზატორი",
     "alt_en": "Mural of a turbocharger, pistons and a coilover"},
    {"source": "route66", "file": "route-66-wall.webp", "published": False,
     "alt_ka": "კედლის მხატვრობა: შავი კლასიკური ავტომობილი გზაზე",
     "alt_en": "Mural of a black classic car on the open road"},
    {"source": "lubricants", "file": "lubricants-wall.webp", "published": False,
     "alt_ka": "კედლის მხატვრობა: ძრავის ზეთები",
     "alt_en": "Mural about engine oils"},
    {"source": "horse_wide", "file": "horse-wall.webp", "published": False,
     "alt_ka": "კედლის მხატვრობა: ტურბინა, შაშკებიანი დროშები, ყალყზე შემდგარი ცხენი და "
               "ავტონაწილები",
     "alt_en": "Mural of a turbocharger, checkered flags, a prancing horse and car parts"},
    {"source": "horse", "file": "horse-close-wall.webp", "published": False,
     "alt_ka": "კედლის მხატვრობა ახლოდან: ყალყზე შემდგარი ცხენი, დისკი, ზამბარა და დგუში",
     "alt_en": "Close-up of the mural: a prancing horse, a wheel, a coil spring and a piston"},
    {"source": "engine_close", "file": "engine-close-wall.webp", "published": False,
     "alt_ka": "კედლის მხატვრობა ახლოდან: V12 ძრავა და წარწერები Better People, Build, "
               "Service, Diagnostics, Performance",
     "alt_en": "Closer view of the V12 engine mural with the words Better People, Build, "
               "Service, Diagnostics, Performance"},
    {"source": "lubricants_clear", "file": "lubricants-clear-wall.webp", "published": False,
     "alt_ka": "კედლის მხატვრობა: ძრავის ზეთები, კასრი და დგუში",
     "alt_en": "Mural about engine oils, with an oil drum and a piston"},
)
# The story section's picture: the 1886 wall cut to the paint, with no ceiling above it
# and clear of the corner of the room on the right.  Five to six, as the page sets it.
STORY_CROP = "780x936+78+322"

INK = "#0d0d0e"
PAPER = "#f2f0ec"
WALL = "#D2161C"
STRIPE = "#FFBC00"


class BuildError(RuntimeError):
    """A step failed and its outputs should not be trusted."""


# --------------------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------------------


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(*arguments: str | Path, env: dict[str, str] | None = None) -> str:
    command = [str(argument) for argument in arguments]
    done = subprocess.run(command, capture_output=True, text=True, check=False,
                          env={**os.environ, **(env or {})})
    if done.returncode != 0:
        raise BuildError(f"{command[0]} failed ({done.returncode}): {done.stderr.strip()[:800]}\n"
                         f"  command: {' '.join(command)[:600]}")
    return done.stdout


def magick(*arguments: str | Path) -> None:
    # A fixed seed and no embedded dates: the same inputs give the same bytes.
    run("magick", *arguments)


def brand_dir(value: str | None) -> Path:
    path = Path(value or os.environ.get("GARAGE_BRAND_DIR") or DEFAULT_BRAND_DIR).expanduser()
    if not path.is_dir():
        raise BuildError(f"brand folder not found: {path} (pass --brand DIR)")
    return path


def brand_file(directory: Path, key: str) -> Path:
    name, expected = BRAND_FILES[key]
    path = directory / name
    if not path.is_file():
        raise BuildError(f"missing brand file: {path}")
    actual = sha256(path.read_bytes())
    if actual != expected:
        raise BuildError(f"{name}: sha256 {actual} does not match the pinned {expected}")
    return path


def download(key: str, cache: Path) -> bytes:
    url, expected = DOWNLOADS[key]
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / f"{expected[:16]}-{key}"
    if cached.is_file() and sha256(cached.read_bytes()) == expected:
        return cached.read_bytes()
    print(f"  downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "garage1886-site-assets"})
    with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310 (https, pinned)
        data = response.read()
    actual = sha256(data)
    if actual != expected:
        raise BuildError(f"{url}: sha256 {actual} does not match the pinned {expected}")
    cached.write_bytes(data)
    return data


def write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    print(f"  wrote {path.relative_to(SITE_ROOT)} ({len(data):,} bytes)")


def tool_versions() -> dict[str, str]:
    return {
        "magick": run("magick", "-version").splitlines()[0].replace("Version: ", ""),
        "pdftocairo": subprocess.run(["pdftocairo", "-v"], capture_output=True, text=True,
                                     check=False).stderr.splitlines()[0],
        "fonttools": FONTTOOLS,
    }


def record(directory: Path, name: str, payload: dict, outputs: list[Path]) -> None:
    """Write a manifest beside the outputs: what they came from and their SHA-256."""
    payload = {
        "generated_by": f"{GENERATOR} {payload.pop('step')}",
        **payload,
        "files": [{"path": path.relative_to(directory).as_posix(),
                   "bytes": path.stat().st_size,
                   "sha256": sha256(path.read_bytes())} for path in sorted(outputs)],
    }
    write(directory / name, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def merge_record(directory: Path, name: str, step: str, payload: dict,
                 outputs: list[Path]) -> None:
    """img/ASSETS.json is shared by three steps; each replaces only its own entry."""
    path = directory / name
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    steps = manifest.get("steps", {})
    steps[step] = {
        **payload,
        "files": [{"path": item.relative_to(directory).as_posix(),
                   "bytes": item.stat().st_size,
                   "sha256": sha256(item.read_bytes())} for item in sorted(outputs)],
    }
    manifest = {"generated_by": GENERATOR, "tools": tool_versions(),
                "steps": dict(sorted(steps.items()))}
    write(path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


def sources(*keys: str) -> dict[str, str]:
    return {BRAND_FILES[key][0]: BRAND_FILES[key][1] for key in keys}


# --------------------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------------------


def build_fonts(cache: Path) -> None:
    """Static instances of Noto Sans Georgian and a Latin cut of Barlow Condensed.

    The Georgian files keep Mkhedruli and Mtavruli, so an uppercased Georgian
    headline stays in the same font, and the Lari sign.
    """
    print("fonts")
    env = {"SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH}
    outputs: list[Path] = []
    commands: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory(prefix="site-fonts-") as scratch:
        work = Path(scratch)
        for key in ("noto", "barlow"):
            (work / f"{key}.ttf").write_bytes(download(key, cache))
        for font in FONT_FILES:
            source = work / f"{font['source']}.ttf"
            steps = []
            if font["axes"]:
                instance = work / f"{font['output']}.ttf"
                axes = [f"{tag}={value}" for tag, value in font["axes"].items()]
                run("uvx", "--from", FONTTOOLS, "fonttools", "varLib.instancer", source,
                    *axes, "-o", instance, "-q", env=env)
                steps.append("fonttools varLib.instancer " + " ".join(axes))
                source = instance
            if font.get("capitals"):
                remapped = work / f"{font['output']}.capitals.ttf"
                run("uvx", "--from", FONTTOOLS, "python", "-c", CAPITALS_SCRIPT, source,
                    remapped, env=env)
                steps.append("cmap: U+10D0-10FA and U+10FD-10FF drawn with the glyphs of "
                             "U+1C90-1CBA and U+1CBD-1CBF; name table renamed")
                source = remapped
            target = FONTS / font["output"]
            target.parent.mkdir(parents=True, exist_ok=True)
            subset = ["--unicodes=" + font["unicodes"], "--layout-features=" + font["features"],
                      "--flavor=woff2", "--no-hinting", "--desubroutinize",
                      "--name-IDs=1,2,3,4,6", "--drop-tables+=DSIG,STAT,MVAR,HVAR,gvar,fvar,avar"]
            run("uvx", "--from", FONTTOOLS, "pyftsubset", source, *subset,
                f"--output-file={target}", env=env)
            steps.append("pyftsubset " + " ".join(subset))
            commands[font["output"]] = steps
            outputs.append(target)
            print(f"  wrote fonts/{font['output']} ({target.stat().st_size:,} bytes)")
    for key, name in (("noto_ofl", "OFL-NotoSansGeorgian.txt"),
                      ("barlow_ofl", "OFL-BarlowCondensed.txt")):
        write(FONTS / name, download(key, cache))
        outputs.append(FONTS / name)
    record(FONTS, "FONTS.json", {
        "step": "fonts", "tool": FONTTOOLS, "source_date_epoch": SOURCE_DATE_EPOCH,
        "licence": "SIL Open Font License 1.1",
        "sources": {key: {"url": DOWNLOADS[key][0], "sha256": DOWNLOADS[key][1]}
                    for key in ("noto", "barlow", "noto_ofl", "barlow_ofl")},
        "versions": {"Noto Sans Georgian": "2.005", "Barlow Condensed": "1.408"},
        "commands": commands,
    }, outputs)


# --------------------------------------------------------------------------------------
# Icon sprite
# --------------------------------------------------------------------------------------


_ICON = re.compile(
    r'''<svg xmlns="http://www\.w3\.org/2000/svg" viewBox="0 0 256 256" fill="currentColor">'''
    r'''(?P<shapes>(?:<(?:path|circle|rect|line|polyline|polygon|ellipse)'''
    r'''(?: [a-z][a-z-]*="[^"<>&]*")*/>)+)</svg>''')


def build_icons(cache: Path) -> None:
    """One SVG sprite of Phosphor regular icons, used as `<use href="sprite.svg#name">`."""
    print("icons")
    tarball = download("phosphor", cache)
    algorithm, _, expected = PHOSPHOR_INTEGRITY.partition("-")
    digest = base64.b64encode(hashlib.new(algorithm, tarball).digest()).decode()
    if digest != expected:
        raise BuildError("phosphor: npm integrity mismatch")
    symbols = []
    standalone: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        package = json.load(archive.extractfile("package/package.json"))
        if package.get("version") != PHOSPHOR_VERSION or package.get("license") != "MIT":
            raise BuildError("phosphor: unexpected version or licence")
        licence = archive.extractfile("package/LICENSE").read()
        for name in ICON_NAMES:
            member = archive.extractfile(f"package/assets/regular/{name}.svg")
            if member is None:
                raise BuildError(f"phosphor: no regular icon named {name}")
            # No XML parser: the file must be exactly the shape Phosphor ships, an svg
            # element holding plain shapes, or the build stops.
            found = _ICON.fullmatch(member.read().decode("utf-8").strip())
            if found is None:
                raise BuildError(f"phosphor: {name} is not a plain 256 x 256 icon")
            symbols.append(f'<symbol id="{name}" viewBox="0 0 256 256">{found["shapes"]}</symbol>')
            if name in STANDALONE_ICONS:
                standalone[name] = (
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" '
                    f'fill="{STANDALONE_ICONS[name]}">{found["shapes"]}</svg>\n')
    sprite = ('<svg xmlns="http://www.w3.org/2000/svg">\n'
              f"<!-- Phosphor Icons {PHOSPHOR_VERSION}, regular weight. MIT licence. -->\n"
              + "\n".join(symbols) + "\n</svg>\n")
    write(ICONS / "sprite.svg", sprite)
    write(ICONS / "LICENSE.txt", licence)
    for name, markup in standalone.items():
        write(ICONS / f"{name}.svg", markup)
    record(ICONS, "VENDOR.json", {
        "step": "icons", "package": "@phosphor-icons/core", "version": PHOSPHOR_VERSION,
        "licence": "MIT", "source": DOWNLOADS["phosphor"][0], "integrity": PHOSPHOR_INTEGRITY,
        "sha256": DOWNLOADS["phosphor"][1], "weight": "regular", "icons": list(ICON_NAMES),
    }, [ICONS / "sprite.svg", ICONS / "LICENSE.txt",
        *(ICONS / f"{name}.svg" for name in standalone)])


# --------------------------------------------------------------------------------------
# Logo
# --------------------------------------------------------------------------------------

_NUMBER = re.compile(r"-?\d+\.\d+")
_PATH_DATA = r"[MLCZ0-9 .\-]*"
_VIEW_BOX = re.compile(r'<svg [^>]*viewBox="0 0 ([\d.]+) ([\d.]+)"')
_GLYPH = re.compile(rf'<g id="(glyph-\d+-\d+)">\s*(?:<path d="({_PATH_DATA})"/>\s*)?</g>')
_SHAPE = re.compile(rf'<path fill-rule="evenodd" fill="(rgb\([^"]*\))" fill-opacity="1" '
                    rf'd="({_PATH_DATA})"/>')
_RUN = re.compile(r'<g fill="(rgb\([^"]*\))" fill-opacity="1">(.*?)</g>', re.DOTALL)
_USE = re.compile(r'<use xlink:href="#(glyph-\d+-\d+)" x="([\d.\-]+)" y="([\d.\-]+)"/>')


def _hex(colour: str) -> str:
    parts = re.findall(r"([\d.]+)%", colour)
    return "#" + "".join(f"{round(float(part) * 2.55):02X}" for part in parts)


def _round(path: str, places: int = 1) -> str:
    def shorten(match: re.Match) -> str:
        text = f"{float(match.group(0)):.{places}f}".rstrip("0").rstrip(".")
        return "0" if text in ("-0", "") else text
    return re.sub(r"\s+", " ", _NUMBER.sub(shorten, path)).strip()


def _bounds(path: str) -> tuple[float, float, float, float]:
    numbers = [float(value) for value in re.findall(r"-?\d+\.?\d*", path)]
    xs, ys = numbers[0::2], numbers[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def read_logo(pdf: Path, scratch: Path) -> dict:
    """The logo's four layers as pdftocairo writes them: the white sticker backing, the
    black car and keyline, the yellow wordmark glyphs and the dark tagline glyphs."""
    svg = scratch / "logo.svg"
    run("pdftocairo", "-svg", pdf, svg)
    # pdftocairo writes a fixed, flat shape; it is matched as text rather than parsed.
    text = svg.read_text(encoding="utf-8")
    box = _VIEW_BOX.search(text)
    glyphs = {name: data or "" for name, data in _GLYPH.findall(text)}
    shapes = _SHAPE.findall(text)
    runs: dict[str, list[tuple[str, float, float]]] = {}
    for fill, body in _RUN.findall(text):
        for name, x, y in _USE.findall(body):
            runs.setdefault(_hex(fill), []).append((name, float(x), float(y)))
    if box is None or len(shapes) != 2 or set(runs) != {"#FFBC00", "#231F20"}:
        raise BuildError("logo: the PDF no longer has the layers this script expects")
    if any(name not in glyphs for run_ in runs.values() for name, _, _ in run_):
        raise BuildError("logo: a glyph the wordmark uses is missing")
    return {"width": float(box[1]), "height": float(box[2]), "glyphs": glyphs, "runs": runs,
            "backing": shapes[0][1], "car": shapes[1][1], "car_fill": _hex(shapes[1][0])}


def _glyph_markup(logo: dict, colour: str, used: set[str]) -> str:
    uses = []
    for name, x, y in logo["runs"][colour]:
        if logo["glyphs"].get(name):
            used.add(name)
            uses.append(f'<use href="#{name}" x="{x:.2f}" y="{y:.2f}"/>')
    return f'<g fill="{colour}">{"".join(uses)}</g>'


def _defs(logo: dict, used: set[str]) -> str:
    body = "".join(f'<path id="{name}" d="{_round(logo["glyphs"][name], 2)}"/>'
                   for name in sorted(used))
    return f"<defs>{body}</defs>"


def build_logo(directory: Path) -> None:
    """The logo as designed, the yellow wordmark alone for the dark nav, and favicons cut
    from the wordmark's own G.  Nothing is redrawn or typeset."""
    print("logo")
    outputs: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="site-logo-") as scratch:
        work = Path(scratch)
        logo = read_logo(brand_file(directory, "logo"), work)
        width, height = round(logo["width"], 2), round(logo["height"], 2)

        used: set[str] = set()
        yellow = _glyph_markup(logo, "#FFBC00", used)
        dark = _glyph_markup(logo, "#231F20", used)
        full = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">'
                f"{_defs(logo, used)}"
                f'<path fill="#FFFFFF" fill-rule="evenodd" d="{_round(logo["backing"])}"/>'
                f'<path fill="{logo["car_fill"]}" fill-rule="evenodd" d="{_round(logo["car"])}"/>'
                f"{yellow}{dark}</svg>\n")
        write(IMAGES / "logo-full.svg", full)
        outputs.append(IMAGES / "logo-full.svg")

        # The wordmark's box: every yellow glyph placed at its position in the logo.
        boxes = []
        for name, x, y in logo["runs"]["#FFBC00"]:
            if logo["glyphs"].get(name):
                left, top, right, bottom = _bounds(logo["glyphs"][name])
                boxes.append((x + left, y + top, x + right, y + bottom))
        left = min(box[0] for box in boxes)
        top = min(box[1] for box in boxes)
        right = max(box[2] for box in boxes)
        bottom = max(box[3] for box in boxes)
        used = set()
        yellow = _glyph_markup(logo, "#FFBC00", used)
        box = f"{left:.2f} {top:.2f} {right - left:.2f} {bottom - top:.2f}"
        write(IMAGES / "wordmark.svg",
              f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}">'
              f"{_defs(logo, used)}{yellow}</svg>\n")
        outputs.append(IMAGES / "wordmark.svg")
        print(f"  wordmark box {right - left:.1f} x {bottom - top:.1f}")

        # Favicon: the wordmark's G on an ink square.
        name = logo["runs"]["#FFBC00"][0][0]
        g_left, g_top, g_right, g_bottom = _bounds(logo["glyphs"][name])
        scale = 44 / (g_right - g_left)
        shift_x = (64 - 44) / 2 - g_left * scale
        shift_y = (64 - (g_bottom - g_top) * scale) / 2 - g_top * scale
        favicon = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                   '<rect width="64" height="64" fill="#0E0E0F"/>'
                   f'<path fill="#FFBC00" transform="translate({shift_x:.3f} {shift_y:.3f}) '
                   f'scale({scale:.5f})" d="{_round(logo["glyphs"][name], 2)}"/></svg>\n')
        write(IMAGES / "favicon.svg", favicon)
        outputs.append(IMAGES / "favicon.svg")

        # Raster favicons are drawn from the same path, so they need no SVG renderer.
        points = _round(logo["glyphs"][name], 2)
        big = work / "favicon-512.png"
        factor = 512 / 64
        magick("-size", "512x512", "xc:#0E0E0F", "-fill", STRIPE, "-stroke", "none",
               "-draw", (f"translate {shift_x * factor:.3f},{shift_y * factor:.3f} "
                         f"scale {scale * factor:.5f},{scale * factor:.5f} path '{points}'"),
               big)
        magick(big, "-resize", "180x180", "-strip", "-define", "png:exclude-chunks=date,time",
               IMAGES / "apple-touch-icon.png")
        magick(big, "-define", "icon:auto-resize=48,32,16", IMAGES / "favicon.ico")
        for item in ("apple-touch-icon.png", "favicon.ico"):
            outputs.append(IMAGES / item)
            print(f"  wrote img/{item} ({(IMAGES / item).stat().st_size:,} bytes)")
    merge_record(IMAGES, "ASSETS.json", "logo", {"sources": sources("logo")}, outputs)


# --------------------------------------------------------------------------------------
# Wall art
# --------------------------------------------------------------------------------------

# Paint that is not the wall: 1 where a pixel is black, white or any colour but the
# wall's red, 0 on the red itself.  `(r - max(g, b))` is about 0.5 on the red in every
# light and near 0 on paint.
NOT_RED = ("dd = u.r - max(u.g,u.b); aa = (dd-{low})/({high}-{low}); "
           "aa = aa<0?0:(aa>1?1:aa); 1 - aa*aa*(3-2*aa)")


def _burst(photo: Path, work: Path) -> Path:
    """A radial spray burst built from the real rays around the gold logo mural.

    The mural's logo sits in the middle of the painted burst, so the burst cannot be
    lifted whole.  Instead the photo is unwrapped around the logo's centre (angle
    across, radius down), the two clean fans of rays left and right of the lettering
    are cut out as tiles, the red wall between rays is made transparent, and the tiles
    are laid around a full turn and wrapped back into a burst with a solid black core.
    """
    unwrapped = work / "burst-unwrapped.png"
    magick(photo, "-virtual-pixel", "Black", "-distort", "DePolar", "520,0,490,900",
           "-resize", "2880x1040!", "+repage", unwrapped)
    west, east = work / "rays-west.png", work / "rays-east.png"
    magick(unwrapped, "-crop", "330x290+500+630", "+repage", "-resize", "288x290!", west)
    magick(unwrapped, "-crop", "480x250+1915+670", "+repage", "-resize", "288x290!", east)
    strip = work / "rays-strip.png"
    tiles: list[str | Path] = []
    for index in range(5):
        tile = west if index % 2 == 0 else east
        tiles += [tile, "(", tile, "-flop", ")"]  # mirrored pairs meet without a seam
    magick(*tiles, "+append", "+repage", "-level", "6%,96%", "-modulate", "100,70", strip)

    paint, column, fade, alpha, colour = (work / f"rays-{name}.png" for name in
                                          ("paint", "column", "fade", "alpha", "colour"))
    magick(strip, "-fx", NOT_RED.format(low="0.12", high="0.40"), "-colorspace", "Gray", paint)
    # Ray length varies by angle: a blurred column average, shifted so it is not tied to
    # the brightness of the ray under it.
    magick(strip, "-colorspace", "Gray", "-scale", "2880x1!", "-roll", "+173+0",
           "-blur", "10x5", "-auto-level", "-scale", "2880x290!", column)
    magick(column, "-fx", ("tt = j/(h-1); ll = 0.30 + 0.70*u; aa = (ll - tt)/0.34; "
                           "aa<0?0:(aa>1?1:aa*aa*(3-2*aa))"), fade)
    magick(paint, fade, "-compose", "Multiply", "-composite",
           "-fx", "tt = j/(h-1); kk = 1 - tt/0.16; kk = kk<0?0:kk; max(u, kk)", alpha)
    magick(strip, "-fx", "tt = j/(h-1); kk = 1 - tt/0.26; kk = kk<0?0:kk; u*(1-kk) + 0.05*kk",
           colour)
    rays = work / "rays.png"
    magick(colour, alpha, "-alpha", "off", "-compose", "CopyOpacity", "-composite", rays)
    square = work / "burst-square.png"
    magick("-size", "2880x330", f"xc:{INK}", rays, "-background", "none", "-append",
           "+repage", "-resize", "1280x1280!", square)
    wrapped = work / "burst-wrapped.png"
    magick(square, "-virtual-pixel", "HorizontalTile", "-background", "none",
           "-distort", "Polar", "640", "+repage", wrapped)
    # A second, black-only copy turned half way fills the gaps with black spikes.
    burst = work / "burst.png"
    magick(wrapped, "(", "+clone", "-alpha", "extract", "-background", INK, "-alpha", "shape",
           "-virtual-pixel", "Transparent", "-distort", "SRT", "0.9,197", ")", "+swap",
           "-background", "none", "-compose", "Over", "-composite",
           "-resize", "1280x960!", burst)
    return burst


def _burst_masks(burst: Path, work: Path) -> tuple[Path, Path]:
    """The burst as two alpha masks: everything painted, and the white of the rays.

    The page paints them with CSS masks (a black layer and a paper layer), which keeps
    the art out of Largest Contentful Paint so the headline stays the LCP element.
    """
    everything, white = work / "mask-k.png", work / "mask-w.png"
    magick(burst, "-alpha", "extract", everything)
    magick(burst, "-alpha", "off", "-colorspace", "Gray",
           "-fx", "ll=(u-0.16)/(0.70-0.16); ll<0?0:(ll>1?1:ll)",
           everything, "-compose", "Multiply", "-composite", white)
    return everything, white


def _mask_webp(mask: Path, size: str, target: Path) -> None:
    magick("-size", size, "xc:black", "(", mask, "-resize", f"{size}!", ")",
           "-alpha", "off", "-compose", "CopyOpacity", "-composite",
           "-define", "webp:alpha-quality=70", "-define", "webp:method=6", "-quality", "40",
           target)


def _lettering(photo: Path, work: Path) -> Path:
    """The hand-painted "More than a Garage" with the rays under it, lifted off the red.

    The wall is photographed at an angle; four degrees levels the lettering and the
    beam it stands on.  The crop stops above the beam (the brake mural below carries
    other companies' marks) and left of the wall's corner.
    """
    level = work / "lettering-level.png"
    magick(photo, "-crop", "960x460+0+260", "+repage", "-virtual-pixel", "Edge",
           "-distort", "SRT", "-4", "+repage", "-crop", "800x232+148+118", "+repage", level)
    paint, edge, corner, alpha, colour = (work / f"lettering-{name}.png" for name in
                                          ("paint", "edge", "corner", "alpha", "colour"))
    # Not-red paint, and anything dark: the black rays stay solid in shadow.
    magick(level, "-fx", ("dd = u.r - max(u.g,u.b); aa = (dd-0.10)/(0.40-0.10); "
                          "aa = aa<0?0:(aa>1?1:aa); nr = 1 - aa*aa*(3-2*aa); "
                          "mx = max(u.r,max(u.g,u.b)); kk = (0.52-mx)/(0.52-0.20); "
                          "kk = kk<0?0:(kk>1?1:kk); max(nr, kk)"), "-colorspace", "Gray", paint)
    magick("-size", "800x232", "xc:", "-fx",
           ("ex = min(i/120, (w-1-i)/14); ey = j/18; ee = min(ex,ey); "
            "ee<0?0:(ee>1?1:ee*ee*(3-2*ee))"), edge)
    magick("-size", "800x232", "xc:white", "-fill", "black", "-draw",
           "polygon 756,0 800,0 800,158 772,150", "-blur", "0x5", corner)
    magick(paint, edge, "-compose", "Multiply", "-composite",
           corner, "-compose", "Multiply", "-composite", alpha)
    # Remove the wall's red from half-covered pixels so the paint keeps its own colour.
    magick(level, alpha, "-fx",
           ("aa = v.r; cc = aa > 0.03 ? (u - (1-aa)*channel(0.78,0.07,0.09))/aa : 0; "
            "cc<0?0:(cc>1?1:cc)"), "-level", "6%,88%", colour)
    lettering = work / "lettering.png"
    magick(colour, alpha, "-alpha", "off", "-compose", "CopyOpacity", "-composite", lettering)
    return lettering


def _sheen(photo: Path, work: Path) -> Path:
    """How the light really falls on the red wall, as a darkening overlay.

    Only the red pixels of the band above the lettering are averaged (the blur is
    alpha-weighted), so lettering and ceiling leave no trace.  The result is black
    with up to 30% alpha: laid over either wall red it only ever darkens.
    """
    band, red, light, alpha = (work / f"sheen-{name}.png" for name in
                               ("band", "red", "light", "alpha"))
    magick(photo, "-crop", "830x300+130+250", "+repage", band)
    magick(band, "-fx", "dd = u.r - max(u.g,u.b); aa = (dd-0.30)/0.15; aa<0?0:(aa>1?1:aa)",
           "-colorspace", "Gray", "-morphology", "Erode", "Disk:3", red)
    magick(band, red, "-alpha", "off", "-compose", "CopyOpacity", "-composite",
           "-channel", "RGBA", "-blur", "0x28", "+channel", "-alpha", "off", light)
    magick(light, "-colorspace", "Gray", "-blur", "0x12", "-auto-level", "-negate",
           "-evaluate", "Multiply", "0.30", "-resize", "96x40!", alpha)
    sheen = work / "sheen.png"
    magick("-size", "96x40", "xc:#070101", alpha, "-alpha", "off",
           "-compose", "CopyOpacity", "-composite", sheen)
    return sheen


def build_art(directory: Path) -> None:
    print("art")
    outputs: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="site-art-") as scratch:
        work = Path(scratch)
        burst = _burst(brand_file(directory, "logo_wall"), work)
        everything, white = _burst_masks(burst, work)
        for mask, name in ((everything, "burst-k"), (white, "burst-w")):
            for size, suffix in (("960x720", ""), ("640x480", "-640")):
                target = IMAGES / f"{name}{suffix}.webp"
                _mask_webp(mask, size, target)
                outputs.append(target)

        brakes = brand_file(directory, "brakes")
        target = IMAGES / "lettering.webp"
        magick(_lettering(brakes, work), "-define", "webp:alpha-quality=75",
               "-define", "webp:method=6", "-quality", "72", target)
        outputs.append(target)

        target = IMAGES / "sheen.webp"
        magick(_sheen(brakes, work), "-define", "webp:alpha-quality=60",
               "-define", "webp:method=6", "-quality", "50", target)
        outputs.append(target)

        # Film grain: a seeded noise tile, light or dark specks at low alpha.
        noise, strength, speck = (work / f"grain-{name}.png" for name in
                                  ("noise", "strength", "speck"))
        magick("-size", "128x128", "xc:gray50", "-seed", "1886", "+noise", "Gaussian",
               "-colorspace", "Gray", "-blur", "0x0.4", "-auto-level", noise)
        magick(noise, "-fx", "abs(u-0.5)*2", "-evaluate", "Multiply", "0.55",
               "-posterize", "6", strength)
        magick(noise, "-threshold", "50%", speck)
        target = IMAGES / "grain.png"
        magick(speck, strength, "-alpha", "off", "-compose", "CopyOpacity", "-composite",
               "-colorspace", "Gray", "-strip", "-define", "png:compression-level=9",
               "-define", "png:color-type=4", "-define", "png:exclude-chunks=date,time", target)
        outputs.append(target)
        for item in outputs:
            print(f"  wrote img/{item.name} ({item.stat().st_size:,} bytes)")
    merge_record(IMAGES, "ASSETS.json", "art",
                 {"sources": sources("logo_wall", "brakes")}, outputs)


# --------------------------------------------------------------------------------------
# Photos
# --------------------------------------------------------------------------------------


def build_photos(directory: Path) -> None:
    print("photos")
    outputs: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="site-photos-") as scratch:
        work = Path(scratch)
        # The services tile: the Service mural down to the yellow stripe and the dado.
        mural = brand_file(directory, "service")
        for width, name, quality in ((960, "service-mural.webp", "74"),
                                     (640, "service-mural-640.webp", "72")):
            target = IMAGES / name
            magick(mural, "-crop", "960x830+0+440", "+repage", "-resize", f"{width}x",
                   "-strip", "-define", "webp:method=6", "-quality", quality, target)
            outputs.append(target)

        # The story picture: the wall that tells where the name comes from.
        benz = brand_file(directory, "benz")
        for width, name, quality in ((780, "story-mural.webp", "78"),
                                     (520, "story-mural-520.webp", "76")):
            target = IMAGES / name
            magick(benz, "-crop", STORY_CROP, "+repage", "-resize", f"{width}x",
                   "-strip", "-define", "webp:method=6", "-quality", quality, target)
            outputs.append(target)

        manifest = []
        for photo in GALLERY:
            target = IMAGES / "gallery" / photo["file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            magick(brand_file(directory, photo["source"]), "-auto-orient", "-resize",
                   "1280x1280>", "-strip", "-define", "webp:method=6", "-quality", "80", target)
            size = run("magick", "identify", "-format", "%w %h", target).split()
            manifest.append({"file": photo["file"], "width": int(size[0]),
                             "height": int(size[1]), "alt_ka": photo["alt_ka"],
                             "alt_en": photo["alt_en"], "published": photo["published"]})
            outputs.append(target)
        for item in outputs:
            print(f"  wrote img/{item.relative_to(IMAGES)} ({item.stat().st_size:,} bytes)")
        write(IMAGES / "gallery" / "manifest.json",
              json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        outputs.append(IMAGES / "gallery" / "manifest.json")

        # Open Graph card: the logo on its burst on the red wall, stripe and dado below.
        burst = _burst(brand_file(directory, "logo_wall"), work)
        everything, white = _burst_masks(burst, work)
        layers = []
        for mask, colour in ((everything, INK), (white, PAPER)):
            layer = work / f"og-{mask.stem}.png"
            magick("-size", "1280x960", f"xc:{colour}", mask, "-alpha", "off",
                   "-compose", "CopyOpacity", "-composite", "-resize", "860x", layer)
            layers.append(layer)
        sheen = _sheen(brand_file(directory, "brakes"), work)
        run("pdftocairo", "-png", "-transp", "-singlefile", "-scale-to-x", "1240",
            "-scale-to-y", "-1", brand_file(directory, "logo"), work / "og-logo")
        target = IMAGES / "og.jpg"
        magick("-size", "1200x548", f"xc:{WALL}",
               "(", sheen, "-filter", "Triangle", "-resize", "1200x548!", ")", "-composite",
               layers[0], "-gravity", "center", "-geometry", "+0-4", "-composite",
               layers[1], "-gravity", "center", "-geometry", "+0-4", "-composite",
               "(", work / "og-logo.png", "-resize", "620x", ")",
               "-gravity", "center", "-geometry", "+0-4", "-composite",
               "-size", "1200x22", f"xc:{STRIPE}", "-append",
               "-size", "1200x60", "xc:#0E0E0F", "-append",
               "-strip", "-sampling-factor", "4:4:4", "-quality", "86", target)
        outputs.append(target)
        print(f"  wrote img/og.jpg ({target.stat().st_size:,} bytes)")
    merge_record(IMAGES, "ASSETS.json", "photos",
                 {"sources": sources("logo", "logo_wall", "brakes", "service", "engine",
                                     "turbo", "route66", "lubricants", "benz", "horse",
                                     "horse_wide", "engine_close", "lubricants_clear")},
                 outputs)


# --------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------


def check() -> int:
    """Every recorded file exists and still has its recorded SHA-256."""
    problems: list[str] = []
    counted = 0
    for directory, name in ((FONTS, "FONTS.json"), (ICONS, "VENDOR.json"),
                            (IMAGES, "ASSETS.json")):
        path = directory / name
        if not path.is_file():
            problems.append(f"{path.relative_to(SITE_ROOT)}: missing (run `all`)")
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        groups = (list(manifest["steps"].values()) if "steps" in manifest else [manifest])
        for group in groups:
            for entry in group["files"]:
                target = directory / entry["path"]
                counted += 1
                if not target.is_file():
                    problems.append(f"{target.relative_to(SITE_ROOT)}: missing")
                elif sha256(target.read_bytes()) != entry["sha256"]:
                    problems.append(f"{target.relative_to(SITE_ROOT)}: sha256 mismatch")
    gallery = IMAGES / "gallery" / "manifest.json"
    if gallery.is_file():
        for entry in json.loads(gallery.read_text(encoding="utf-8")):
            photo = gallery.parent / entry["file"]
            if not photo.is_file():
                problems.append(f"gallery: {entry['file']} is listed but missing")
            elif photo.stat().st_size > 300_000:
                problems.append(f"gallery: {entry['file']} is over 300 KB")
    sprite = ICONS / "sprite.svg"
    if sprite.is_file():
        text = sprite.read_text(encoding="utf-8")
        for name in ICON_NAMES:
            if f'<symbol id="{name}"' not in text:
                problems.append(f"sprite: icon {name} is missing")
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        return 1
    print(f"ok: {counted} recorded files match their SHA-256")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("step", choices=("all", "fonts", "icons", "logo", "art", "photos",
                                         "check"))
    parser.add_argument("--brand", help="folder with garage.pdf and the wall photos")
    parser.add_argument("--cache", default=str(Path(os.environ.get(
        "XDG_CACHE_HOME", "~/.cache")).expanduser() / "garage1886-site-assets"),
        help="where pinned downloads are kept")
    arguments = parser.parse_args(argv)
    if arguments.step == "check":
        return check()
    needed = {"fonts": ("uvx",), "icons": (), "logo": ("pdftocairo", "magick"),
              "art": ("magick",), "photos": ("magick", "pdftocairo")}
    steps = list(needed) if arguments.step == "all" else [arguments.step]
    try:
        for tool in sorted({tool for step in steps for tool in needed[step]}):
            if shutil.which(tool) is None:
                raise BuildError(f"`{tool}` is not installed")
        cache = Path(arguments.cache)
        for step in steps:
            if step == "fonts":
                build_fonts(cache)
            elif step == "icons":
                build_icons(cache)
            else:
                directory = brand_dir(arguments.brand)
                {"logo": build_logo, "art": build_art, "photos": build_photos}[step](directory)
    except BuildError as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    return check() if arguments.step == "all" else 0


if __name__ == "__main__":
    raise SystemExit(main())
