#!/usr/bin/env python3
"""Extract, compile, and validate Qt translation catalogs."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TS_PATH = (
    ROOT
    / "satellite_debug_tool"
    / "i18n"
    / "translations"
    / "satellite_debug_tool_zh_CN.ts"
)
QM_PATH = TS_PATH.with_suffix(".qm")
SOURCE_PATHS = [
    ROOT / "satellite_debug_tool" / "main.py",
    ROOT / "satellite_debug_tool" / "ui",
]
PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}|%n|%\d+")
CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _tool(name: str) -> str:
    executable_dir = Path(sys.executable).resolve().parent
    candidates = [
        executable_dir / name,
        executable_dir / f"{name}.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    path = shutil.which(name)
    if path is not None:
        return path
    raise RuntimeError(
        f"{name} was not found next to {sys.executable} or on PATH. "
        "Install the project's PySide6 dependency first."
    )


def _lupdate(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _tool("pyside6-lupdate"),
        "-extensions",
        "py",
        "-no-obsolete",
        "-tr-function-alias",
        (
            "tr+=tr,tr+=set_translatable_text,tr+=set_translatable_tooltip,"
            "tr+=_set_para_status,tr+=_set_ota_status,tr+=_set_para_row_status,"
            "tr+=_ota_finish,tr+=_show_error,"
            "tr+=_set_local_status,"
            "QT_TR_NOOP+=tr_source,"
            "QT_TR_N_NOOP+=trn,QT_TR_N_NOOP+=set_translatable_n_text"
        ),
        "-source-language",
        "en_US",
        "-target-language",
        "zh_CN",
        *map(str, SOURCE_PATHS),
        "-ts",
        str(output),
    ]
    subprocess.run(command, cwd=ROOT, check=True)


def _lrelease(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_tool("pyside6-lrelease"), str(source), "-qm", str(output)],
        cwd=ROOT,
        check=True,
    )


def update() -> None:
    _lupdate(TS_PATH)
    _lrelease(TS_PATH, QM_PATH)


def _messages(path: Path) -> dict[tuple[str, str, bool], ET.Element]:
    root = ET.parse(path).getroot()
    messages: dict[tuple[str, str, bool], ET.Element] = {}
    for context in root.findall("context"):
        context_name = context.findtext("name", default="")
        for message in context.findall("message"):
            translation = message.find("translation")
            if translation is not None and translation.get("type") in {
                "obsolete",
                "vanished",
            }:
                continue
            source = message.findtext("source", default="")
            key = (context_name, source, message.get("numerus") == "yes")
            messages[key] = message
    return messages


def check() -> None:
    errors: list[str] = []
    if not TS_PATH.exists():
        raise RuntimeError(f"Missing translation source: {TS_PATH}")
    if not QM_PATH.exists():
        raise RuntimeError(f"Missing compiled translation: {QM_PATH}")

    committed = _messages(TS_PATH)
    reverse: dict[str, str] = {}
    for (_context, source, numerus), message in committed.items():
        if not source:
            errors.append("Translation catalog contains an empty source")
            continue
        if CJK_RE.search(source):
            errors.append(f"English source contains CJK text: {source!r}")

        translation = message.find("translation")
        if translation is None:
            errors.append(f"Missing translation node: {source!r}")
            continue
        if translation.get("type") == "unfinished":
            errors.append(f"Unfinished translation: {source!r}")
            continue

        if numerus:
            values = [(node.text or "") for node in translation.findall("numerusform")]
            if not values or any(not value.strip() for value in values):
                errors.append(f"Empty numerus translation: {source!r}")
        else:
            values = [translation.text or ""]
            if not values[0].strip():
                errors.append(f"Empty translation: {source!r}")

        source_placeholders = set(PLACEHOLDER_RE.findall(source))
        for value in values:
            translated_placeholders = set(PLACEHOLDER_RE.findall(value))
            if source_placeholders != translated_placeholders:
                errors.append(
                    f"Placeholder mismatch for {source!r}: "
                    f"{source_placeholders} != {translated_placeholders}"
                )
            previous = reverse.get(value)
            if previous is not None and previous != source:
                errors.append(
                    f"Ambiguous translated text {value!r}: {previous!r} / {source!r}"
                )
            reverse[value] = source

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        extracted_ts = temp / "extracted.ts"
        compiled_qm = temp / "compiled.qm"
        _lupdate(extracted_ts)
        extracted = _messages(extracted_ts)
        missing = sorted(set(extracted) - set(committed))
        stale = sorted(set(committed) - set(extracted))
        if missing:
            errors.extend(f"Catalog is missing source: {key[1]!r}" for key in missing)
        if stale:
            errors.extend(f"Catalog contains stale source: {key[1]!r}" for key in stale)

        _lrelease(TS_PATH, compiled_qm)
        if compiled_qm.read_bytes() != QM_PATH.read_bytes():
            errors.append("Compiled QM is not synchronized with the TS source")

    if errors:
        print("Translation catalog check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Translation catalog OK: {len(committed)} messages")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("update", "compile", "check"))
    args = parser.parse_args()

    if args.command == "update":
        update()
    elif args.command == "compile":
        _lrelease(TS_PATH, QM_PATH)
    else:
        check()


if __name__ == "__main__":
    main()
