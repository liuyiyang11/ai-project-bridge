from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

from ..security import SecurityError, resolve_under


class MetricCollector:
    """Read public metrics files into one small, serializable record shape."""

    _SUPPORTED = frozenset({".json", ".csv", ".txt"})
    _BLOCKED_METADATA_KEYS = frozenset(
        {
            "reasoning",
            "rawreasoning",
            "chainofthought",
            "chain_of_thought",
            "textdelta",
            "raw_chain_of_thought",
            "raw_reasoning",
        }
    )

    def __init__(self, *, max_records: int = 1000, max_file_bytes: int = 10 * 1024 * 1024):
        if isinstance(max_records, bool) or int(max_records) < 1:
            raise ValueError("max_records must be positive")
        if isinstance(max_file_bytes, bool) or int(max_file_bytes) < 1:
            raise ValueError("max_file_bytes must be positive")
        self.max_records = int(max_records)
        self.max_file_bytes = int(max_file_bytes)

    def collect(self, files: Union[Iterable[Path], Path], *, root: Optional[Path] = None) -> list[dict[str, Any]]:
        if isinstance(files, Path):
            candidates = list(files.rglob("*") if files.is_dir() else [files])
        else:
            candidates = [Path(item) for item in files]
        candidates = sorted((path for path in candidates if path.is_file()), key=lambda item: item.as_posix())
        records: list[dict[str, Any]] = []
        for path in candidates:
            if path.suffix.casefold() not in self._SUPPORTED:
                continue
            safe_path = self._trusted_path(path, root)
            try:
                if safe_path.stat().st_size > self.max_file_bytes:
                    continue
                if safe_path.suffix.casefold() == ".json":
                    parsed = self._collect_json(safe_path)
                elif safe_path.suffix.casefold() == ".csv":
                    parsed = self._collect_csv(safe_path)
                else:
                    parsed = self._collect_txt(safe_path)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                continue
            for record in parsed:
                records.append(record)
                if len(records) >= self.max_records:
                    return records
        return records

    def _trusted_path(self, path: Path, root: Optional[Path]) -> Path:
        candidate = Path(path)
        if root is None:
            return candidate.resolve()
        trusted_root = Path(root).resolve()
        if candidate.is_absolute():
            resolved = candidate.resolve()
            try:
                resolved.relative_to(trusted_root)
            except ValueError as exc:
                raise SecurityError("metric path is outside the registered project root") from exc
            return resolved
        return resolve_under(trusted_root, candidate.as_posix())

    def _collect_json(self, path: Path) -> list[dict[str, Any]]:
        value = json.loads(path.read_text(encoding="utf-8"))
        return self._json_records(value)

    def _json_records(self, value: Any, default_name: Optional[str] = None) -> list[dict[str, Any]]:
        if isinstance(value, list):
            output: list[dict[str, Any]] = []
            for item in value:
                output.extend(self._json_records(item, default_name=default_name))
            return output
        if not isinstance(value, Mapping):
            if default_name is None:
                return []
            record = self._record(default_name, value)
            return [record] if record is not None else []
        if "metrics" in value:
            return self._json_records(value["metrics"], default_name=default_name)
        if "metric_name" in value or ("name" in value and "value" in value):
            name = value.get("metric_name", value.get("name"))
            record = self._record(name, value.get("value"), step=value.get("step"), metadata=value.get("metadata"), extra=value)
            return [record] if record is not None else []

        output = []
        for name, item in value.items():
            if name in {"step", "metadata"}:
                continue
            if isinstance(item, Mapping) and "value" in item:
                record = self._record(name, item.get("value"), step=item.get("step", value.get("step")), metadata=item.get("metadata"), extra=item)
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                record = self._record(name, item, step=value.get("step"), metadata=value.get("metadata"))
            else:
                record = None
            if record is not None:
                output.append(record)
        return output

    def _collect_csv(self, path: Path) -> list[dict[str, Any]]:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return []
            output = []
            for row in reader:
                name = row.get("metric_name") or row.get("name")
                if not name:
                    continue
                metadata = self._metadata_value(row.get("metadata"))
                extra = {
                    key: value
                    for key, value in row.items()
                    if key not in {"metric_name", "name", "value", "step", "metadata"} and value not in {None, ""}
                }
                for key, value in extra.items():
                    if str(key).casefold() not in self._BLOCKED_METADATA_KEYS:
                        metadata[str(key)] = self._public_value(value)
                record = self._record(name, row.get("value"), step=row.get("step"), metadata=metadata)
                if record is not None:
                    output.append(record)
            return output

    def _collect_txt(self, path: Path) -> list[dict[str, Any]]:
        output = []
        pattern = re.compile(r"^\s*([A-Za-z0-9_.\-/]+)\s*(?:=|:|\s)\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)")
        step_pattern = re.compile(r"\bstep\s*[=:]\s*(\d+)", re.IGNORECASE)
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = pattern.match(stripped)
            if not match:
                continue
            step_match = step_pattern.search(stripped)
            record = self._record(match.group(1), match.group(2), step=step_match.group(1) if step_match else None)
            if record is not None:
                output.append(record)
        return output

    @staticmethod
    def _metadata_value(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return MetricCollector._public_value(value)
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {"metadata": value[:4000]}
            return MetricCollector._public_value(parsed) if isinstance(parsed, Mapping) else {"metadata": value[:4000]}
        return {}

    @classmethod
    def _public_value(cls, value: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "<truncated>"
        if isinstance(value, Mapping):
            return {
                str(key): cls._public_value(item, depth + 1)
                for key, item in list(value.items())[:100]
                if str(key).casefold() not in cls._BLOCKED_METADATA_KEYS
            }
        if isinstance(value, list):
            return [cls._public_value(item, depth + 1) for item in value[:100]]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:4000]

    def _record(
        self,
        name: Any,
        value: Any,
        *,
        step: Any = None,
        metadata: Any = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        if not isinstance(name, str) or not name.strip():
            return None
        parsed_value = self._number(value)
        if parsed_value is None:
            return None
        parsed_step = self._step(step)
        merged = self._metadata_value(metadata)
        if extra:
            for key, item in extra.items():
                if key not in {"metric_name", "name", "value", "step", "metadata"}:
                    if str(key).casefold() not in self._BLOCKED_METADATA_KEYS:
                        merged.setdefault(str(key), self._public_value(item))
        return {
            "metric_name": name.strip(),
            "value": parsed_value,
            "step": parsed_step,
            "metadata": merged,
        }

    @staticmethod
    def _number(value: Any) -> Optional[Union[float, int]]:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return value if math.isfinite(float(value)) else None
        if isinstance(value, str):
            try:
                parsed = float(value.strip())
            except ValueError:
                return None
            if not math.isfinite(parsed):
                return None
            return int(parsed) if parsed.is_integer() else parsed
        return None

    @classmethod
    def _step(cls, value: Any) -> Optional[Union[float, int]]:
        return cls._number(value)
