"""Small config loader — pure stdlib + optional yaml."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List


def _load_yaml(path: str) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:
        yaml = None
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if yaml is not None:
        return yaml.safe_load(text)
    # Minimal YAML: colon-separated, 2-space indent, lists via [a, b] or - a.
    return _tiny_yaml_parse(text)


def _tiny_yaml_parse(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    stack: List[tuple] = [(-1, root)]
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if stripped.startswith("- "):
            item = _coerce_scalar(stripped[2:].strip())
            if not isinstance(parent, list):
                raise ValueError("list item outside list")
            parent.append(item)
            continue
        if ":" not in stripped:
            raise ValueError(f"bad line: {raw}")
        key, _, rest = stripped.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "":
            child: Any = {}
            parent[key] = child
            stack.append((indent, child))
        elif rest.startswith("[") and rest.endswith("]"):
            body = rest[1:-1].strip()
            parent[key] = [_coerce_scalar(x.strip()) for x in body.split(",") if x.strip()]
        else:
            parent[key] = _coerce_scalar(rest)
    return root


def _coerce_scalar(text: str) -> Any:
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if text.lower() in ("null", "none", "~"):
        return None
    try:
        if "." in text or "e" in text.lower():
            return float(text)
        return int(text)
    except ValueError:
        return text.strip('"').strip("'")


@dataclass
class TCSimConfig:
    chunk: Dict[str, Any] = field(default_factory=dict)
    scheduler: Dict[str, Any] = field(default_factory=dict)
    uarch: Dict[str, Any] = field(default_factory=dict)
    model: Dict[str, Any] = field(default_factory=dict)
    train: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> "TCSimConfig":
        if path.endswith(".json"):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            data = _load_yaml(path)
        return cls(
            chunk=data.get("chunk", {}),
            scheduler=data.get("scheduler", {}),
            uarch=data.get("uarch", {}),
            model=data.get("model", {}),
            train=data.get("train", {}),
        )

    @property
    def K(self) -> int:
        return int(self.chunk.get("K", 256))

    @property
    def epsilon(self) -> float:
        return float(self.scheduler.get("epsilon", 500.0))


DEFAULT_CFG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "mvp.yaml")
