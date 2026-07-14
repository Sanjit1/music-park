from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any, Iterable, Iterator


META_FILE = "meta.json"
ARTISTS_FILE = "artists.jsonl"
CONNECTORS_FILE = "connectors.jsonl"
ADJACENCY_FILE = "adjacency.pkl"
NAME_INDEX_FILE = "name_index.json"

KNOWN_FILES = [
    META_FILE,
    ARTISTS_FILE,
    CONNECTORS_FILE,
    ADJACENCY_FILE,
    NAME_INDEX_FILE,
]


def prepare_out_dir(out_dir: str | Path) -> Path:
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    for name in KNOWN_FILES:
        target = path / name
        if target.exists():
            target.unlink()
    return path


def write_json(path: str | Path, data: Any) -> None:
    with Path(path).open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_jsonl_item(handle, item: dict[str, Any]) -> None:
    handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True))
    handle.write("\n")


def iter_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{line_no}: invalid JSONL") from exc


def jsonl_node_value(line: str) -> str | None:
    marker = '"node": "'
    start = line.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = line.find('"', start)
    if end == -1:
        return None
    return line[start:end]


def write_pickle(path: str | Path, data: Any) -> None:
    with Path(path).open("wb") as handle:
        pickle.dump(data, handle, protocol=pickle.HIGHEST_PROTOCOL)


def read_pickle(path: str | Path) -> Any:
    with Path(path).open("rb") as handle:
        return pickle.load(handle)


def load_jsonl_by_node(path: str | Path, wanted_nodes: Iterable[str] | None = None) -> dict[str, Any]:
    wanted = set(wanted_nodes) if wanted_nodes is not None else None
    items: dict[str, Any] = {}
    if wanted is not None:
        if not wanted:
            return items
        with Path(path).open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                node = jsonl_node_value(line)
                if node not in wanted:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"{path}:{line_no}: invalid JSONL") from exc
                if item.get("node") != node:
                    continue
                items[node] = item
                if len(items) == len(wanted):
                    break
        return items

    for item in iter_jsonl(path):
        node = item.get("node")
        if not isinstance(node, str):
            continue
        if wanted is not None and node not in wanted:
            continue
        items[node] = item
        if wanted is not None and len(items) == len(wanted):
            break
    return items
