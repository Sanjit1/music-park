from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CACHE_DB = "data/cache/mcn_cache.sqlite"


SCHEMA = """
CREATE TABLE IF NOT EXISTS path_cache(
  graph_version TEXT NOT NULL,
  source_artist_id INTEGER NOT NULL,
  target_artist_id INTEGER NOT NULL,
  source_name TEXT,
  target_name TEXT,
  hop_count INTEGER,
  path_json TEXT NOT NULL,
  query_count INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(graph_version, source_artist_id, target_artist_id)
);

CREATE TABLE IF NOT EXISTS subpath_counts(
  graph_version TEXT NOT NULL,
  source_artist_id INTEGER NOT NULL,
  target_artist_id INTEGER NOT NULL,
  hop_count INTEGER NOT NULL,
  count INTEGER NOT NULL DEFAULT 1,
  example_path_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(graph_version, source_artist_id, target_artist_id)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_cache_path() -> Path:
    return Path(os.environ.get("MCN_CACHE_DB", DEFAULT_CACHE_DB))


def canonical_pair(source_id: int, target_id: int) -> tuple[int, int, bool]:
    if source_id <= target_id:
        return source_id, target_id, False
    return target_id, source_id, True


def reverse_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(payload))
    payload["source"], payload["target"] = payload["target"], payload["source"]
    payload["raw_path"] = list(reversed(payload.get("raw_path", [])))
    reversed_steps = []
    for step in reversed(payload.get("collapsed_path", [])):
        step = dict(step)
        step["from"], step["to"] = step["to"], step["from"]
        reversed_steps.append(step)
    payload["collapsed_path"] = reversed_steps
    return payload

# For specific artist pairs, we store the path in a canonical order (lower artist ID first). If the source and target are reversed for storage, we need to reverse the payload when retrieving it from the cache.
class PathCache:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else default_cache_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False) # thing
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get_path(
        self, graph_version: str, source_artist_id: int, target_artist_id: int
    ) -> dict[str, Any] | None:
        left_id, right_id, reversed_for_storage = canonical_pair(
            source_artist_id, target_artist_id
        )
        row = self.conn.execute(
            """
            SELECT path_json FROM path_cache
            WHERE graph_version = ? AND source_artist_id = ? AND target_artist_id = ?
            """,
            (graph_version, left_id, right_id),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute(
            """
            UPDATE path_cache
            SET query_count = query_count + 1, updated_at = ?
            WHERE graph_version = ? AND source_artist_id = ? AND target_artist_id = ?
            """,
            (utc_now(), graph_version, left_id, right_id),
        )
        self.conn.commit()
        payload = json.loads(row["path_json"])
        return reverse_payload(payload) if reversed_for_storage else payload

    def store_path(
        self,
        graph_version: str,
        source_artist_id: int,
        target_artist_id: int,
        payload: dict[str, Any],
    ) -> None:
        left_id, right_id, reversed_for_storage = canonical_pair(
            source_artist_id, target_artist_id
        )
        stored_payload = reverse_payload(payload) if reversed_for_storage else payload
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO path_cache(
              graph_version, source_artist_id, target_artist_id, source_name,
              target_name, hop_count, path_json, query_count, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(graph_version, source_artist_id, target_artist_id) DO UPDATE SET
              source_name = excluded.source_name,
              target_name = excluded.target_name,
              hop_count = excluded.hop_count,
              path_json = excluded.path_json,
              query_count = path_cache.query_count + 1,
              updated_at = excluded.updated_at
            """,
            (
                graph_version,
                left_id,
                right_id,
                stored_payload.get("source", {}).get("name"),
                stored_payload.get("target", {}).get("name"),
                stored_payload.get("hop_count"),
                json.dumps(stored_payload, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        self.conn.commit()

    # For paths that havent been cached yet, we look for subpaths.
    def record_subpaths(self, graph_version: str, payload: dict[str, Any]) -> None:
        steps = payload.get("collapsed_path", [])
        if not steps:
            return
        artist_ids = [int(steps[0]["from"]["artist_id"])]
        for step in steps:
            artist_ids.append(int(step["to"]["artist_id"]))

        now = utc_now()
        for start in range(len(artist_ids)):
            for end in range(start + 1, len(artist_ids)):
                sub_ids = artist_ids[start : end + 1]
                left_id, right_id, _reversed_for_storage = canonical_pair(
                    sub_ids[0], sub_ids[-1]
                )
                sub_steps = steps[start:end]
                example = {
                    "artist_ids": sub_ids,
                    "collapsed_path": sub_steps,
                }
                self.conn.execute(
                    """
                    INSERT INTO subpath_counts(
                      graph_version, source_artist_id, target_artist_id, hop_count,
                      count, example_path_json, updated_at
                    )
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(graph_version, source_artist_id, target_artist_id)
                    DO UPDATE SET
                      count = count + 1,
                      example_path_json = excluded.example_path_json,
                      updated_at = excluded.updated_at
                    """,
                    (
                        graph_version,
                        left_id,
                        right_id,
                        len(sub_steps),
                        json.dumps(example, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
        self.conn.commit()

    def stats(self, graph_version: str, limit: int = 10) -> dict[str, Any]:
        count = self.conn.execute(
            "SELECT COUNT(*) AS count FROM path_cache WHERE graph_version = ?",
            (graph_version,),
        ).fetchone()["count"]
        paths = self.conn.execute(
            """
            SELECT source_artist_id, target_artist_id, source_name, target_name,
                   hop_count, query_count, updated_at
            FROM path_cache
            WHERE graph_version = ?
            ORDER BY query_count DESC, updated_at DESC
            LIMIT ?
            """,
            (graph_version, limit),
        ).fetchall()
        subpaths = self.conn.execute(
            """
            SELECT source_artist_id, target_artist_id, hop_count, count,
                   example_path_json, updated_at
            FROM subpath_counts
            WHERE graph_version = ?
            ORDER BY count DESC, updated_at DESC
            LIMIT ?
            """,
            (graph_version, limit),
        ).fetchall()
        return {
            "db_path": str(self.db_path),
            "cached_full_paths": count,
            "top_cached_full_paths": [dict(row) for row in paths],
            "top_subpaths": [
                {
                    **{key: row[key] for key in row.keys() if key != "example_path_json"},
                    "example_path": json.loads(row["example_path_json"]),
                }
                for row in subpaths
            ],
        }
