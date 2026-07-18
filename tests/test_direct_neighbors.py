from __future__ import annotations

import json
from pathlib import Path

from music_mcn.direct_neighbors import direct_artist_neighbors, write_direct_artist_neighbors
from music_mcn.search import Graph


def tiny_graph() -> Graph:
    return Graph(
        Path("tiny"),
        adjacency={
            "a:1": ["r:1", "m:1"],
            "a:2": ["r:1"],
            "a:3": ["m:1"],
            "r:1": ["a:1", "a:2"],
            "m:1": ["a:1", "a:3"],
        },
        artists={
            "a:1": {"node": "a:1", "artist_id": 1, "name": "Alpha", "degree": 2},
            "a:2": {"node": "a:2", "artist_id": 2, "name": "Beta", "degree": 1},
            "a:3": {"node": "a:3", "artist_id": 3, "name": "Gamma", "degree": 1},
        },
        name_index={"by_mbid": {}, "by_name": {"alpha": ["a:1"]}},
        meta={},
    )


def test_direct_artist_neighbors_returns_direct_artists_only():
    payload = direct_artist_neighbors(tiny_graph(), "Alpha")

    assert payload["resolved"]["node"] == "a:1"
    assert payload["neighbor_count"] == 2
    assert [neighbor["node"] for neighbor in payload["neighbors"]] == ["a:2", "a:3"]


def test_write_direct_artist_neighbors_writes_json(tmp_path):
    output_path = write_direct_artist_neighbors(tiny_graph(), "Alpha", tmp_path)

    assert output_path.exists()
    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data["resolved"]["node"] == "a:1"
    assert data["neighbor_count"] == 2