from __future__ import annotations

from pathlib import Path

from music_mcn.dag import build_shortest_dag


CANONICAL_PATH = ["a:1", "r:10", "a:2", "r:12", "a:9"]


class TinyDagGraph:
    def __init__(self):
        self.graph_dir = Path("tiny")
        self.meta = {"graph_version": "tiny-dag-v1"}
        self.adjacency = {
            "a:1": ["r:10", "r:11", "r:16"],
            "r:10": ["a:1", "a:2"],
            "r:11": ["a:1", "a:3"],
            "a:2": ["r:10", "r:12", "r:14"],
            "a:3": ["r:11", "r:13"],
            "r:12": ["a:2", "a:9"],
            "r:13": ["a:3", "a:9"],
            "a:9": ["r:12", "r:13", "r:15"],
            "r:14": ["a:2", "a:5"],
            "a:5": ["r:14", "r:15"],
            "r:15": ["a:5", "a:9"],
            "r:16": ["a:1", "a:4"],
            "a:4": ["r:16"],
        }
        self.artists = {
            "a:1": {"node": "a:1", "artist_id": 1, "name": "Source", "degree": 3, "artist_type": "Person"},
            "a:2": {"node": "a:2", "artist_id": 2, "name": "Middle A", "degree": 3, "artist_type": "Group"},
            "a:3": {"node": "a:3", "artist_id": 3, "name": "Middle B", "degree": 2, "artist_type": "Group"},
            "a:4": {"node": "a:4", "artist_id": 4, "name": "Dead End", "degree": 1, "artist_type": "Person"},
            "a:5": {"node": "a:5", "artist_id": 5, "name": "Long Route", "degree": 2, "artist_type": "Group"},
            "a:9": {"node": "a:9", "artist_id": 9, "name": "Target", "degree": 3, "artist_type": "Person"},
        }
        self.connectors = {
            node: {
                "node": node,
                "type": "recording",
                "recording_id": int(node.split(":", 1)[1]),
                "title": f"Recording {node}",
            }
            for node in self.adjacency
            if node.startswith("r:")
        }

    def ensure_connectors(self, nodes):
        return None

    def path_payload(self, source, target, path, preserve_direction=True):
        return {
            "source": self.artists[source],
            "target": self.artists[target],
            "hop_count": (len(path) - 1) // 2,
            "raw_path": [{"node": node} for node in path],
            "collapsed_path": [],
        }


def layer_nodes(payload):
    return {
        layer["index"]: {node["node"] for node in layer["nodes"]}
        for layer in payload["layers"]
    }


def test_shortest_dag_layers_and_full_shortest_nodes():
    graph = TinyDagGraph()
    payload = build_shortest_dag(
        graph,
        "a:1",
        "a:9",
        canonical_path=CANONICAL_PATH,
        limit_per_layer=10,
        max_total_nodes=50,
    )

    layers = layer_nodes(payload)
    assert list(layers) == [0, 1, 2, 3, 4]
    assert layers[0] == {"a:1"}
    assert layers[1] == {"r:10", "r:11"}
    assert layers[2] == {"a:2", "a:3"}
    assert layers[3] == {"r:12", "r:13"}
    assert layers[4] == {"a:9"}
    assert payload["layers"][0]["nodes"][0]["artist_type"] == "Person"
    assert payload["layers"][2]["nodes"][0]["artist_type"] == "Group"
    assert payload["raw_distance"] == 4
    assert payload["mcn_hops"] == 2


def test_shortest_dag_excludes_dead_and_longer_routes():
    graph = TinyDagGraph()
    payload = build_shortest_dag(
        graph,
        "a:1",
        "a:9",
        canonical_path=CANONICAL_PATH,
        limit_per_layer=10,
        max_total_nodes=50,
    )

    returned = set().union(*layer_nodes(payload).values())
    assert "r:16" not in returned
    assert "a:4" not in returned
    assert "r:14" not in returned
    assert "a:5" not in returned
    assert "r:15" not in returned


def test_capping_keeps_canonical_shortest_path():
    graph = TinyDagGraph()
    payload = build_shortest_dag(
        graph,
        "a:1",
        "a:9",
        canonical_path=CANONICAL_PATH,
        limit_per_layer=1,
        max_total_nodes=5,
    )

    layers = layer_nodes(payload)
    for layer in layers.values():
        assert len(layer) <= 1
    for index, node in enumerate(CANONICAL_PATH):
        assert node in layers[index]

    canonical_edges = {
        ("artist:1", "recording:10"),
        ("recording:10", "artist:2"),
        ("artist:2", "recording:12"),
        ("recording:12", "artist:9"),
    }
    returned_edges = {
        (edge["source"], edge["target"])
        for edge in payload["edges"]
    }
    assert canonical_edges <= returned_edges
    assert payload["truncated"] is True


def test_edges_only_advance_one_layer():
    graph = TinyDagGraph()
    payload = build_shortest_dag(
        graph,
        "a:1",
        "a:9",
        canonical_path=CANONICAL_PATH,
        limit_per_layer=10,
        max_total_nodes=50,
    )

    id_to_layer = {
        node["id"]: layer["index"]
        for layer in payload["layers"]
        for node in layer["nodes"]
    }
    for edge in payload["edges"]:
        assert id_to_layer[edge["target"]] == id_to_layer[edge["source"]] + 1


def test_count_cap_comes_from_env(monkeypatch):
    monkeypatch.setenv("MCN_DAG_COUNT_CAP", "1")
    graph = TinyDagGraph()
    payload = build_shortest_dag(
        graph,
        "a:1",
        "a:9",
        canonical_path=CANONICAL_PATH,
        limit_per_layer=10,
        max_total_nodes=50,
    )

    source_node = payload["layers"][0]["nodes"][0]
    target_node = payload["layers"][-1]["nodes"][0]
    assert source_node["score"] == 1
    assert target_node["score"] == 1
    assert payload["stats"]["count_cap"] == 1


def test_unhydrated_alternate_connector_nodes_are_excluded():
    graph = TinyDagGraph()
    graph.connectors.pop("r:13", None)
    payload = build_shortest_dag(
        graph,
        "a:1",
        "a:9",
        canonical_path=CANONICAL_PATH,
        limit_per_layer=10,
        max_total_nodes=50,
    )

    returned_nodes = {
        node["node"]
        for layer in payload["layers"]
        for node in layer["nodes"]
    }
    assert "r:13" not in returned_nodes
    assert "a:3" not in returned_nodes
    assert payload["stats"]["dropped_unhydrated_nodes"] == 1
