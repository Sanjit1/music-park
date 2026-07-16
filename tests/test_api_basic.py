from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from music_mcn.api import create_app
from music_mcn.search import SearchLimitExceeded


EXPECTED_FILES = ["meta.json", "artists.jsonl", "connectors.jsonl", "adjacency.pkl"]


def graph_dir_or_skip() -> Path:
    raw = os.environ.get("MCN_GRAPH_DIR")
    if not raw:
        pytest.skip("Set MCN_GRAPH_DIR to run API tests against a local graph artifact")
    graph_dir = Path(raw)
    if not all((graph_dir / name).exists() for name in EXPECTED_FILES):
        pytest.skip(f"MCN_GRAPH_DIR does not point at a complete graph artifact: {graph_dir}")
    return graph_dir


@pytest.fixture(scope="module")
def real_app(tmp_path_factory):
    graph_dir = graph_dir_or_skip()
    cache_db = tmp_path_factory.mktemp("cache") / "mcn_cache.sqlite"
    return create_app(graph_dir, cache_db)


@pytest.fixture(scope="module")
def client(real_app):
    with TestClient(real_app) as test_client:
        yield test_client


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["artist_count"] > 0


def test_bowie_freddie_path_and_cache(client):
    params = {"source": "David Bowie", "target": "Freddie Mercury"}
    first = client.get("/mcn/path", params=params)
    assert first.status_code == 200
    assert first.json()["hop_count"] == 2

    second = client.get("/mcn/path", params=params)
    assert second.status_code == 200
    data = second.json()
    assert data["hop_count"] == 2
    assert data["cache"] == "hit"


def test_bowie_freddie_shortest_dag(real_app):
    data = route(real_app, "/mcn/shortest-dag")(
        source="David Bowie",
        target="Freddie Mercury",
        limit_per_layer=10,
        max_total_nodes=150,
        include_debug=False,
    )
    assert data["mcn_hops"] == 2
    assert data["raw_distance"] == 4
    assert data["layers"][0]["nodes"][0]["type"] == "artist"
    assert data["layers"][-1]["nodes"][0]["type"] == "artist"
    assert data["edges"]


class TinyGraph:
    def __init__(self, limit: bool = False):
        self.graph_dir = Path("tiny")
        self.meta = {
            "graph_version": "tiny-v1",
            "artist_nodes": 2,
            "connector_nodes": 1,
            "raw_bipartite_edges": 2,
        }
        self.artists = {
            "a:1": {"node": "a:1", "artist_id": 1, "name": "A", "degree": 1},
            "a:2": {"node": "a:2", "artist_id": 2, "name": "B", "degree": 1},
        }
        self.adjacency = {
            "a:1": ["r:1"],
            "r:1": ["a:1", "a:2"],
            "a:2": ["r:1"],
        }
        self.connectors = {
            "r:1": {
                "node": "r:1",
                "type": "recording",
                "recording_id": 1,
                "title": "Tiny Song",
            }
        }
        self.name_index = {"by_mbid": {}, "by_name": {"a": ["a:1"], "b": ["a:2"]}}
        self.limit = limit

    def find_artist(self, query):
        return {"A": "a:1", "B": "a:2"}[query]

    def search_artist_candidates(self, query, limit=10):
        return [
            {
                **self.artists["a:1"],
                "score": 90000,
                "match_reason": "name_exact",
                "matched_value": query,
            }
        ][:limit]

    def shortest_path(self, source, target, max_search_ms=None, max_expanded_nodes=None):
        if self.limit:
            raise SearchLimitExceeded(
                "limit", expanded_nodes=3, max_expanded_nodes=2, max_search_ms=1
            )
        return [source, "r:1", target]

    def ensure_connectors(self, nodes):
        return None

    def path_payload(self, source, target, path):
        return {
            "source": self.artists[source],
            "target": self.artists[target],
            "hop_count": 1,
            "raw_path": [{"node": node} for node in path],
            "collapsed_path": [
                {
                    "from": self.artists[source],
                    "via": {"node": "r:1", "type": "recording", "title": "Tiny Song"},
                    "to": self.artists[target],
                    "explanation": "recording: Tiny Song",
                }
            ],
        }


def route(app, path: str):
    return next(item.endpoint for item in app.routes if getattr(item, "path", None) == path)


def tiny_app(monkeypatch, tmp_path, graph):
    monkeypatch.setattr("music_mcn.api.Graph.load", lambda _path: graph)
    return create_app("tiny", tmp_path / "cache.sqlite")


def test_cache_stats_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MCN_ENABLE_CACHE_STATS", raising=False)
    app = tiny_app(monkeypatch, tmp_path, TinyGraph())
    with pytest.raises(HTTPException) as exc:
        route(app, "/cache/stats")(limit=10, x_mcn_admin_token=None)
    assert exc.value.status_code == 404


def test_cache_stats_enabled_with_admin_token(monkeypatch, tmp_path):
    monkeypatch.setenv("MCN_ENABLE_CACHE_STATS", "true")
    monkeypatch.setenv("MCN_ADMIN_TOKEN", "secret")
    app = tiny_app(monkeypatch, tmp_path, TinyGraph())

    with pytest.raises(HTTPException) as exc:
        route(app, "/cache/stats")(limit=10, x_mcn_admin_token="wrong")
    assert exc.value.status_code == 403

    data = route(app, "/cache/stats")(limit=10, x_mcn_admin_token="secret")
    assert data["graph_version"] == "tiny-v1"
    assert data["cached_full_paths"] == 0


def test_health_with_tiny_graph(monkeypatch, tmp_path):
    app = tiny_app(monkeypatch, tmp_path, TinyGraph())
    data = route(app, "/health")()
    assert data["status"] == "ok"
    assert data["limits"]["max_concurrent_searches"] >= 1


def test_artist_search_route_uses_graph_search(monkeypatch, tmp_path):
    app = tiny_app(monkeypatch, tmp_path, TinyGraph())
    data = route(app, "/artists/search")(q="A", limit=10)
    assert data["candidates"][0]["node"] == "a:1"
    assert data["candidates"][0]["match_reason"] == "name_exact"


def test_shortest_dag_with_tiny_graph(monkeypatch, tmp_path):
    app = tiny_app(monkeypatch, tmp_path, TinyGraph())
    data = route(app, "/mcn/shortest-dag")(
        source="A",
        target="B",
        limit_per_layer=10,
        max_total_nodes=150,
        include_debug=False,
    )
    assert data["raw_distance"] == 2
    assert data["mcn_hops"] == 1
    assert [layer["index"] for layer in data["layers"]] == [0, 1, 2]
    assert data["edges"] == [
        {"source": "artist:1", "target": "recording:1"},
        {"source": "recording:1", "target": "artist:2"},
    ]


def test_shortest_dag_falls_back_to_canonical_when_dag_expansion_is_limited(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MCN_MAX_EXPANDED_NODES", "1")
    app = tiny_app(monkeypatch, tmp_path, TinyGraph())
    data = route(app, "/mcn/shortest-dag")(
        source="A",
        target="B",
        limit_per_layer=10,
        max_total_nodes=150,
        include_debug=False,
    )
    assert data["truncated"] is True
    assert data["stats"]["partial_reason"] == "dag_search_limit_exceeded"
    assert data["raw_distance"] == 2
    assert data["mcn_hops"] == 1
    assert data["edges"] == [
        {"source": "artist:1", "target": "recording:1"},
        {"source": "recording:1", "target": "artist:2"},
    ]


def test_search_limit_handling(monkeypatch, tmp_path):
    monkeypatch.setenv("MCN_MAX_SEARCH_MS", "1")
    monkeypatch.setenv("MCN_MAX_EXPANDED_NODES", "2")
    app = tiny_app(monkeypatch, tmp_path, TinyGraph(limit=True))

    with pytest.raises(HTTPException) as exc:
        route(app, "/mcn/path")(source="A", target="B")
    assert exc.value.status_code == 504
    assert exc.value.detail["error"] == "search_limit_exceeded"
