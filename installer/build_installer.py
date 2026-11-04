#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Build of the AmviBackUp installer (PyInstaller + Inno Setup 6.3+).

Run from the project folder (amvibackup_X_Y_Z.py and amvibackup_languages.py are next to each other):

py installer\build_installer.py --app amvibackup_22_0.py

Modes:
--check          only check the dictionary and generate the installer texts (no PyInstaller, no Inno)
--skip-freeze    do not run PyInstaller, use the already built installer\generated\dist folder
--no-download    do not download the Inno translation files from the internet
--inno PATH      path to ISCC.exe or to the Inno Setup folder if the automatic search did not find it

The installer\generated folder is created by the build; it does not need to be edited by hand.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any

INSTALLER_DIR = Path(__file__).resolve().parent
PROJECT_DIR = INSTALLER_DIR.parent
GENERATED_DIR = INSTALLER_DIR / "generated"
LANGUAGES_DIR = INSTALLER_DIR / "languages"
LEGAL_DIR = PROJECT_DIR / "legal"
OUTPUT_DIR = INSTALLER_DIR / "output"
ISS_FILE = INSTALLER_DIR / "AmviBackUp.iss"
ICON_FILE = INSTALLER_DIR / "AmviBackUp.ico"
APP_NAME = "AmviBackUp"
LANGUAGE_MODULE_NAME = "amvibackup_languages.py"
PLACEHOLDER_NUMBERS = {"old": 1, "new": 2}

# Application language code -> translation file of the standard Inno wizard texts.
STANDARD_LANGUAGE_FILES = {
    "en": "Default.isl",
    "ru": "Russian.isl",
    "zh": "ChineseSimplified.isl",
    "hi": "Hindi.islu",
    "es": "Spanish.isl",
    "fr": "French.isl",
    "ar": "Arabic.isl",
}

DOWNLOAD_BASES = (
    "https://raw.githubusercontent.com/jrsoftware/issrc/is-6_7_1/Files/Languages",
    "https://raw.githubusercontent.com/jrsoftware/issrc/main/Files/Languages",
)


class BuildError(Exception):
    """A clear build error: the text is shown to the user as is."""


# ----------------------------------------------------------------------------
# Version and dictionary
# ----------------------------------------------------------------------------
def pad_version_numbers(numeric: str) -> str:
    """21.1 -> 21.1.0.0 (Windows and Inno Setup require four version components)."""
    parts = numeric.split(".")
    return ".".join((parts + ["0"] * 4)[:4])


def read_app_version(app_file: Path) -> tuple[str, str]:
    """Returns (full version, four numbers) from the application's APP_VERSION."""
    text = Path(app_file).read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise BuildError(f"The APP_VERSION line was not found in {app_file}.")
    full = match.group(1)
    numeric_match = re.match(r"^(\d+(?:\.\d+){1,3})(?![\d.])", full)
    if not numeric_match:
        raise BuildError(f"APP_VERSION '{full}' must start with two to four numbers, for example 21.1-beta or 0.19.9.6-alpha.")
    return full, pad_version_numbers(numeric_match.group(1))


def load_language_data(path: Path) -> Any:
    if not Path(path).is_file():
        raise BuildError(f"Dictionary not found: {path}.")
    spec = importlib.util.spec_from_file_location("amvibackup_languages_for_build", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # a dictionary with a syntax error must not produce a long traceback
        raise BuildError(f"The dictionary {path} cannot be loaded: {exc}") from exc
    return module


def installer_keys(translations: dict) -> list[str]:
    return sorted(key for key in translations["en"] if key.startswith("installer."))


def message_name(key: str) -> str:
    """installer.update_welcome -> AmviUpdateWelcome (name of an Inno custom message)."""
    rest = key.split(".", 1)[1]
    return "Amvi" + "".join(part[:1].upper() + part[1:] for part in re.split(r"[_.]", rest) if part)


def inno_text(text: str) -> str:
    """Dictionary text -> [CustomMessages] value: {old}->%1, {new}->%2, line break->%n, %->%%."""
    result = text.replace("%", "%%")

    def replace(match: re.Match) -> str:
        number = PLACEHOLDER_NUMBERS.get(match.group(1))
        if number is None:
            raise BuildError(f"Unknown parameter {{{match.group(1)}}} in the installer text: {text[:60]!r}")
        return f"%{number}"

    result = re.sub(r"\{(\w*)\}", replace, result)
    if "{" in result or "}" in result:
        raise BuildError(f"Extra curly brace in the installer text: {text[:60]!r}")
    return result.replace("\r\n", "\n").replace("\n", "%n")


def check_dictionary(language_data: Any) -> list[str]:
    """List of dictionary problems for the installer; an empty list means everything is fine."""
    problems: list[str] = []
    translations = language_data.TRANSLATIONS
    problems.extend(str(item) for item in language_data.validate())
    supported = set(language_data.supported_codes())
    if supported != set(STANDARD_LANGUAGE_FILES):
        problems.append(f"dictionary languages {sorted(supported)} do not match the installer languages {sorted(STANDARD_LANGUAGE_FILES)}")
    if "en" not in translations:
        return problems + ["the dictionary has no English language"]
    keys = installer_keys(translations)
    if len(keys) < 5:
        problems.append(f"the dictionary has too few installer.* keys: {len(keys)}")
    names = [message_name(key) for key in keys]
    if len(set(names)) != len(names):
        problems.append("installer message names are duplicated")
    for code in sorted(supported):
        table = translations.get(code, {})
        for key in keys:
            value = table.get(key)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{code}: empty or missing {key}")
                continue
            if code != "en" and value == translations["en"][key]:
                problems.append(f"{code}: {key} is the same as English (not translated)")
            try:
                inno_text(value)
            except BuildError as exc:
                problems.append(f"{code}: {key}: {exc}")
    return problems


# ----------------------------------------------------------------------------
# Generating files for Inno
# ----------------------------------------------------------------------------
def generate_messages(translations: dict, out_dir: Path) -> dict[str, Path]:
    """Writes <code>.isl (UTF-8 with BOM) with a [CustomMessages] section for each language."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = installer_keys(translations)
    files: dict[str, Path] = {}
    for code in STANDARD_LANGUAGE_FILES:
        lines = ["; Created by build_installer.py from amvibackup_languages.py. Do not edit.", "[CustomMessages]"]
        for key in keys:
            lines.append(f"{message_name(key)}={inno_text(translations[code][key])}")
        path = out_dir / f"{code}.isl"
        path.write_bytes(b"\xef\xbb\xbf" + ("\n".join(lines) + "\n").encode("utf-8"))
        files[code] = path
    return files


def _looks_like_language_file(data: bytes) -> bool:
    return len(data) > 200 and (b"[LangOptions]" in data or b"[Messages]" in data)


def ensure_language_file(filename: str, inno_dir: Path | None, allow_download: bool) -> Path:
    """Puts the Inno translation file into installer\\languages: takes it from the installed Inno or downloads it."""
    target = LANGUAGES_DIR / filename
    if target.is_file() and target.stat().st_size > 0:
        return target
    if inno_dir is not None:
        for sub in ("Languages", "Languages/Unofficial"):
            source = Path(inno_dir) / sub / filename
            if source.is_file():
                LANGUAGES_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                return target
    errors: list[str] = []
    if allow_download:
        for base in DOWNLOAD_BASES:
            for sub in ("", "/Unofficial"):
                url = f"{base}{sub}/{filename}"
                try:
                    with urllib.request.urlopen(url, timeout=30) as response:
                        data = response.read()
                except (urllib.error.URLError, OSError) as exc:
                    errors.append(f"{url}: {exc}")
                    continue
                if _looks_like_language_file(data):
                    LANGUAGES_DIR.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    return target
                errors.append(f"{url}: the file does not look like an Inno translation file")
    hint = "" if allow_download else " (downloading is disabled)"
    raise BuildError(
        f"Translation file {filename} not found{hint}. Put it manually into {LANGUAGES_DIR} "
        f"(in Inno Setup it is in the Languages or Languages\\Unofficial folder, or in the repository "
        f"github.com/jrsoftware/issrc, folder Files/Languages)."
        + (f" Download attempts: {'; '.join(errors[-2:])}" if errors else "")
    )


def write_languages_include(out_dir: Path, inno_dir: Path | None, allow_download: bool = True) -> Path:
    """Writes languages.iss: a [Languages] section with the standard Inno file and the generated texts."""
    out_dir = Path(out_dir)
    lines = ["; Created by build_installer.py. Do not edit.", "[Languages]"]
    for code, filename in STANDARD_LANGUAGE_FILES.items():
        if code == "en":
            standard = "compiler:Default.isl"
        else:
            ensure_language_file(filename, inno_dir, allow_download)
            standard = f"languages\\{filename}"
        license_name = license_file_name(code)
        if not (LEGAL_DIR / license_name).is_file():
            raise BuildError(f"License file not found: {LEGAL_DIR / license_name}.")
        lines.append(f'Name: "{code}"; MessagesFile: "{standard},generated\\{code}.isl"; LicenseFile: "..\\legal\\{license_name}"')
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "languages.iss"
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return path


# ----------------------------------------------------------------------------
# Icon (no external libraries): the letter A on a violet background
# ----------------------------------------------------------------------------
def _inside_convex(polygon: list[tuple[float, float]], x: float, y: float) -> bool:
    sign = 0
    for index, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(index + 1) % len(polygon)]
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        if cross != 0:
            current = 1 if cross > 0 else -1
            if sign and current != sign:
                return False
            sign = current
    return True


_LETTER_SHAPES = (
    [(0.43, 0.17), (0.57, 0.17), (0.37, 0.83), (0.22, 0.83)],
    [(0.43, 0.17), (0.57, 0.17), (0.78, 0.83), (0.63, 0.83)],
    [(0.31, 0.58), (0.69, 0.58), (0.69, 0.69), (0.31, 0.69)],
)


def _in_rounded_square(x: float, y: float, radius: float = 0.2) -> bool:
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        return False
    cx = min(max(x, radius), 1.0 - radius)
    cy = min(max(y, radius), 1.0 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _png(size: int, samples: int) -> bytes:
    background = (109, 124, 255)
    rows = bytearray()
    for py in range(size):
        rows.append(0)
        for px in range(size):
            bg_hits = letter_hits = 0
            for sy in range(samples):
                for sx in range(samples):
                    x = (px + (sx + 0.5) / samples) / size
                    y = (py + (sy + 0.5) / samples) / size
                    if _in_rounded_square(x, y):
                        bg_hits += 1
                        if any(_inside_convex(shape, x, y) for shape in _LETTER_SHAPES):
                            letter_hits += 1
            total = samples * samples
            if bg_hits == 0:
                rows.extend((0, 0, 0, 0))
                continue
            mix = letter_hits / bg_hits
            rows.extend(tuple(round(background[i] * (1 - mix) + 255 * mix) for i in range(3)))
            rows.append(round(255 * bg_hits / total))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")


def create_icon(path: Path) -> None:
    sizes = ((256, 2), (64, 3), (48, 3), (32, 4), (16, 4))
    images = [(size, _png(size, samples)) for size, samples in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    payload = b""
    for size, data in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset + len(payload))
        payload += data
    Path(path).write_bytes(header + entries + payload)


def ensure_icon() -> None:
    if not ICON_FILE.is_file() or ICON_FILE.stat().st_size == 0:
        create_icon(ICON_FILE)
        print(f"  icon created: {ICON_FILE.name}")


# ----------------------------------------------------------------------------
# External programs
# ----------------------------------------------------------------------------
def find_iscc(explicit: str | None) -> Path | None:
    candidates: list[Path] = []
    for raw in (explicit, os.environ.get("ISCC"), os.environ.get("INNO_SETUP")):
        if raw:
            path = Path(raw)
            candidates.append(path if path.suffix.lower() == ".exe" else path / "ISCC.exe")
    found = shutil.which("ISCC")
    if found:
        candidates.append(Path(found))
    roots = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(str(Path(local) / "Programs"))
    for root in filter(None, roots):
        base = Path(root)
        if base.is_dir():
            for folder in sorted(base.glob("Inno Setup*"), reverse=True):
                candidates.append(folder / "ISCC.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def run(command: list[str], title: str, cwd: Path | None = None, timeout: int | None = None) -> None:
    print(f"  > {' '.join(command)}")
    try:
        result = subprocess.run(command, cwd=cwd, timeout=timeout)
    except FileNotFoundError as exc:
        raise BuildError(f"{title}: could not start the program ({exc}).") from exc
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"{title}: timed out ({timeout} s).") from exc
    if result.returncode != 0:
        raise BuildError(f"{title}: the program exited with code {result.returncode}. The full error text is above.")


APP_AUTHOR = "Maxim Dubkov (AmviSoft)"
APP_COPYRIGHT_TEXT = "\u00a9 2026 Maxim Dubkov (AmviSoft)"
LEGAL_FILES = ("LICENSE.txt", "THIRD_PARTY_NOTICES.txt")


def license_file_name(code: str) -> str:
    """One license text (the official English MIT text) is shown in every installer language."""
    return "LICENSE.txt"


def check_legal_files() -> None:
    missing = [name for name in LEGAL_FILES if not (LEGAL_DIR / name).is_file()]
    if missing:
        raise BuildError(f"The folder {LEGAL_DIR} is missing files: {', '.join(missing)}.")


def write_version_info(path: Path, full_version: str, numeric_version: str) -> Path:
    """Version resource of AmviBackUp.exe: publisher, copyright and version in the file properties."""
    numbers = tuple(int(part) for part in pad_version_numbers(numeric_version).split("."))
    fields = (
        ("CompanyName", APP_AUTHOR),
        ("FileDescription", APP_NAME),
        ("FileVersion", numeric_version),
        ("InternalName", APP_NAME),
        ("LegalCopyright", APP_COPYRIGHT_TEXT),
        ("OriginalFilename", f"{APP_NAME}.exe"),
        ("ProductName", APP_NAME),
        ("ProductVersion", full_version),
    )
    strings = ",\n        ".join(f"StringStruct({ascii(key)}, {ascii(value)})" for key, value in fields)
    text = (
        "# UTF-8\n"
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        f"    filevers={numbers},\n"
        f"    prodvers={numbers},\n"
        "    mask=0x3f,\n"
        "    flags=0x0,\n"
        "    OS=0x40004,\n"
        "    fileType=0x1,\n"
        "    subtype=0x0,\n"
        "    date=(0, 0)\n"
        "  ),\n"
        "  kids=[\n"
        "    StringFileInfo([\n"
        "      StringTable('040904B0', [\n"
        f"        {strings}\n"
        "      ])\n"
        "    ]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


def collect_package_licenses(dist_app: Path) -> int:
    """Best effort: copy the license files of Qt/PySide6 from the installed packages into legal\\third_party."""
    import importlib.metadata as metadata

    target_root = dist_app / "_internal" / "legal" / "third_party"
    copied = 0
    for package in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6"):
        try:
            distribution = metadata.distribution(package)
        except metadata.PackageNotFoundError:
            continue
        for item in distribution.files or []:
            if not re.match(r"^(LICENSE|COPYING|NOTICE)", Path(str(item)).name, re.IGNORECASE):
                continue
            source = Path(str(distribution.locate_file(item)))
            if not source.is_file():
                continue
            folder = target_root / package
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, folder / source.name)
            copied += 1
    if copied:
        print(f"  copied Qt/PySide6 license files: {copied}")
    else:
        print("  WARNING: Qt/PySide6 license files were not found in the installed packages. "
              "Put the license texts into legal\\third_party manually (see THIRD_PARTY_NOTICES.txt).")
    return copied


def freeze(app_file: Path, language_file: Path) -> Path:
    check = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"], capture_output=True, text=True)
    if check.returncode != 0:
        raise BuildError("PyInstaller is not installed. Run: py -m pip install pyinstaller")
    major = check.stdout.strip().split(".")[0]
    if major.isdigit() and int(major) < 6:
        raise BuildError(f"PyInstaller 6 or newer is required (now {check.stdout.strip()}): py -m pip install --upgrade pyinstaller")
    check_legal_files()
    full_version, numeric_version = read_app_version(app_file)
    version_file = write_version_info(GENERATED_DIR / "version_info.txt", full_version, numeric_version)
    for folder in ("dist", "work", "spec"):
        shutil.rmtree(GENERATED_DIR / folder, ignore_errors=True)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", APP_NAME, "--icon", str(ICON_FILE),
        "--add-data", f"{language_file}{os.pathsep}.",
        "--add-data", f"{LEGAL_DIR}{os.pathsep}legal",
        "--version-file", str(version_file),
        "--distpath", str(GENERATED_DIR / "dist"),
        "--workpath", str(GENERATED_DIR / "work"),
        "--specpath", str(GENERATED_DIR / "spec"),
        str(app_file),
    ]
    support_assets = PROJECT_DIR / "assets" / "support"
    if not (PROJECT_DIR / "amvibackup_support.py").is_file() or not support_assets.is_dir():
        raise BuildError("Required support files are missing: check amvibackup_support.py and assets/support.")
    command.extend(["--add-data", f"{support_assets}{os.pathsep}assets/support",
                    "--hidden-import", "amvibackup_support", "--paths", str(PROJECT_DIR)])
    run(command, "PyInstaller")
    return GENERATED_DIR / "dist" / APP_NAME


def check_frozen_app(dist_app: Path) -> None:
    exe = dist_app / f"{APP_NAME}.exe"
    if not exe.is_file():
        raise BuildError(f"{exe} not found. Run the build without --skip-freeze first.")
    if not (dist_app / "_internal").is_dir():
        raise BuildError(f"{dist_app} has no _internal folder: the installer expects PyInstaller 6 or newer.")
    print("  checking the built program (--smoke-test)...")
    try:
        result = subprocess.run([str(exe), "--smoke-test"], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired as exc:
        raise BuildError("The built program did not answer --smoke-test within 180 seconds.") from exc
    if result.returncode != 0:
        raise BuildError(
            f"--smoke-test of the built program returned code {result.returncode}. "
            f"Output: {(result.stderr or result.stdout or 'empty').strip()[:500]}"
        )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ----------------------------------------------------------------------------
# Main procedure
# ----------------------------------------------------------------------------
def pick_app_file(raw: str | None) -> Path:
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            path = (Path.cwd() / path) if (Path.cwd() / path).is_file() else (PROJECT_DIR / path)
        if not path.is_file():
            raise BuildError(f"Application file not found: {raw}")
        return path.resolve()
    found = sorted(PROJECT_DIR.glob("amvibackup_[0-9]*.py"))
    found = [item for item in found if "_tests" not in item.name]
    if not found:
        raise BuildError("amvibackup_X_Y_Z.py was not found next to the installer folder. Use --app.")
    return found[-1].resolve()


def build(args: argparse.Namespace) -> int:
    print("[1/6] Application and version")
    app_file = pick_app_file(args.app)
    full, numeric = read_app_version(app_file)
    print(f"  {app_file.name}: version {full}")

    print("[2/6] Dictionary and installer texts")
    language_file = PROJECT_DIR / LANGUAGE_MODULE_NAME
    language_data = load_language_data(language_file)
    problems = check_dictionary(language_data)
    if problems:
        raise BuildError("The dictionary failed validation:\n  - " + "\n  - ".join(problems[:20]))
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    generate_messages(language_data.TRANSLATIONS, GENERATED_DIR)
    print(f"  installer texts generated for {len(STANDARD_LANGUAGE_FILES)} languages")

    iscc = None if args.check else find_iscc(args.inno)
    if not args.check and iscc is None:
        raise BuildError("ISCC.exe not found (Inno Setup 6.3 or newer). Install Inno Setup or pass --inno \"path\\to\\ISCC.exe\".")
    inno_dir = iscc.parent if iscc else (Path(args.inno) if args.inno and not str(args.inno).lower().endswith(".exe") else None)

    print("[3/6] Installer wizard languages")
    try:
        write_languages_include(GENERATED_DIR, inno_dir, allow_download=not args.no_download)
        print("  languages.iss created")
    except BuildError as exc:
        if not args.check:
            raise
        print(f"  WARNING (not an error in --check mode): {exc}")
    if args.check:
        print("Check finished: the dictionary and installer texts are fine.")
        return 0

    ensure_icon()
    print("[4/6] Building the program (PyInstaller)")
    dist_app = GENERATED_DIR / "dist" / APP_NAME
    if not args.skip_freeze:
        dist_app = freeze(app_file, language_file)
    check_frozen_app(dist_app)
    collect_package_licenses(dist_app)

    print("[5/6] Building the installer (Inno Setup)")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    command = [str(iscc), f"/DAppVersionFull={full}", f"/DAppVersionNumeric={numeric}", f"/DDistDir={dist_app}", str(ISS_FILE)]
    run(command, "Inno Setup (ISCC)", cwd=INSTALLER_DIR)
    setup = OUTPUT_DIR / f"{APP_NAME}-Setup-{full}.exe"
    if not setup.is_file():
        raise BuildError(f"ISCC finished without an error, but {setup} does not exist.")

    print("[6/6] Checksum")
    digest = sha256_of(setup)
    (OUTPUT_DIR / (setup.name + ".sha256")).write_text(f"{digest} *{setup.name}\n", encoding="utf-8")
    print(f"  {setup}\n  SHA-256: {digest}\n  size: {setup.stat().st_size / (1024 * 1024):.1f} MB")
    print("Done. The installer has not been tested on a clean Windows yet: run the installed-program checks first.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AmviBackUp installer build")
    parser.add_argument("--app", help="application file, for example amvibackup_22_0.py")
    parser.add_argument("--check", action="store_true", help="only check the dictionary and generate the texts")
    parser.add_argument("--skip-freeze", action="store_true", help="do not run PyInstaller")
    parser.add_argument("--no-download", action="store_true", help="do not download Inno translation files")
    parser.add_argument("--inno", help="path to ISCC.exe or to the Inno Setup folder")
    args = parser.parse_args(argv)
    try:
        return build(args)
    except BuildError as exc:
        print(f"\nBUILD ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
