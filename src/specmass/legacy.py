from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import ProcessProgram, ProcessStage


_NATURAL_PART = re.compile(r"(\d+)")


def loads_legacy_json(text: str) -> Any:
    """Read LabVIEW JSON plus the trailing commas found in deployed configs."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_without_trailing_commas(text))


def load_legacy_json(path: str | Path) -> Any:
    return loads_legacy_json(Path(path).read_text(encoding="utf-8-sig"))


def load_program(directory: str | Path) -> ProcessProgram:
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"Program folder does not exist: {root}")

    settings_path = _find_case_insensitive(root, "ScanSettings.msdef")
    settings = load_legacy_json(settings_path) if settings_path else {}
    stage_paths = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_file()
            and path.suffix.lower() == ".msdef"
            and path.name.lower() != "scansettings.msdef"
        ),
        key=lambda path: _natural_key(path.name),
    )
    stages = tuple(
        ProcessStage.from_mapping(load_legacy_json(path), default_name=f"Stage{index}")
        for index, path in enumerate(stage_paths)
    )
    return ProcessProgram(stages=stages, scan_settings=settings)


def load_configuration(directory: str | Path) -> dict[str, Any]:
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"Configuration folder does not exist: {root}")
    result: dict[str, Any] = {}
    for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file():
            continue
        try:
            result[path.name] = load_legacy_json(path)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot parse legacy configuration {path}") from exc
    return result


def _find_case_insensitive(root: Path, name: str) -> Path | None:
    target = name.lower()
    return next((path for path in root.iterdir() if path.name.lower() == target), None)


def _natural_key(name: str) -> tuple[object, ...]:
    return tuple(int(part) if part.isdigit() else part.lower() for part in _NATURAL_PART.split(name))


def _without_trailing_commas(text: str) -> str:
    """Remove only structural trailing commas, never comma text inside strings."""
    result: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            result.append(char)
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                continue
        result.append(char)
    return "".join(result)
