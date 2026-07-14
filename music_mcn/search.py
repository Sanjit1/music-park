from __future__ import annotations

import argparse
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import artifacts


ARTIST_PREFIX = "a:"
RECORDING_PREFIX = "r:"
MEMBERSHIP_PREFIX = "m:"


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().casefold())


class ArtistLookupError(RuntimeError):
    pass


class ArtistNotFoundError(ArtistLookupError):
    def __init__(self, query: str):
        super().__init__(f"No artist found for {query!r}")
        self.query = query


class AmbiguousArtistError(ArtistLookupError):
    def __init__(self, query: str, candidates: list[dict[str, Any]]):
        super().__init__(f"Ambiguous artist name {query!r}")
        self.query = query
        self.candidates = candidates


class SearchLimitExceeded(RuntimeError):
    def __init__(
        self,
        message: str,
        expanded_nodes: int,
        max_expanded_nodes: int | None,
        max_search_ms: int | None,
    ):
        super().__init__(message)
        self.expanded_nodes = expanded_nodes
        self.max_expanded_nodes = max_expanded_nodes
        self.max_search_ms = max_search_ms


class Graph:
    def __init__(
        self,
        graph_dir: Path,
        adjacency: dict[str, list[str]],
        artists: dict[str, dict[str, Any]],
        name_index: dict[str, Any],
        meta: dict[str, Any],
    ):
        self.graph_dir = graph_dir
        self.adjacency = adjacency
        self.artists = artists
        self.name_index = name_index
        self.meta = meta
        self.connectors: dict[str, dict[str, Any]] = {}

    @classmethod
    def load(cls, graph_dir: str | Path) -> "Graph":
        root = Path(graph_dir)
        meta = artifacts.read_json(root / artifacts.META_FILE)
        adjacency = artifacts.read_pickle(root / artifacts.ADJACENCY_FILE)
        artists = artifacts.load_jsonl_by_node(root / artifacts.ARTISTS_FILE)
        name_index = artifacts.read_json(root / artifacts.NAME_INDEX_FILE)
        return cls(root, adjacency, artists, name_index, meta)

    def find_artist(self, name_or_mbid: str) -> str:
        query = name_or_mbid.strip()
        if not query:
            raise ArtistNotFoundError(name_or_mbid)

        by_mbid = self.name_index.get("by_mbid", {})
        mbid_match = by_mbid.get(query.casefold())
        if mbid_match:
            return mbid_match

        by_name = self.name_index.get("by_name", {})
        candidates = list(by_name.get(re.sub(r"\s+", " ", query.strip().casefold()), []))
        if not candidates:
            raise ArtistNotFoundError(query)

        exact = [node for node in candidates if self.artists[node].get("name") == query]
        exact_best = self._unique_best_by_degree(exact)
        if exact_best is not None:
            return exact_best
        if exact:
            raise AmbiguousArtistError(query, self.artist_candidates(exact))

        case_insensitive = [
            node
            for node in candidates
            if str(self.artists[node].get("name", "")).casefold() == query.casefold()
        ]
        case_best = self._unique_best_by_degree(case_insensitive)
        if case_best is not None:
            return case_best
        if case_insensitive:
            raise AmbiguousArtistError(query, self.artist_candidates(case_insensitive))

        candidate_best = self._unique_best_by_degree(candidates)
        if candidate_best is not None:
            return candidate_best
        raise AmbiguousArtistError(query, self.artist_candidates(candidates))

    def _unique_best_by_degree(self, nodes: list[str]) -> str | None: # Pick the artist with the highest degree, or None if there is a tie for best
        if not nodes:
            return None
        ranked = sorted(
            nodes,
            key=lambda node: (
                -int(self.artists.get(node, {}).get("degree", 0)),
                str(self.artists.get(node, {}).get("name", "")).casefold(),
                node,
            ),
        )
        if len(ranked) == 1:
            return ranked[0]
        best_degree = int(self.artists.get(ranked[0], {}).get("degree", 0))
        second_degree = int(self.artists.get(ranked[1], {}).get("degree", 0))
        if best_degree > second_degree:
            return ranked[0]
        return None

    def artist_candidates(self, nodes: list[str], limit: int = 10) -> list[dict[str, Any]]:
        rows = []
        for node in nodes[:limit]:
            artist = self.artists.get(node, {})
            rows.append(
                {
                    "node": node,
                    "name": artist.get("name"),
                    "sort_name": artist.get("sort_name"),
                    "gid": artist.get("gid"),
                    "comment": artist.get("comment"),
                    "degree": artist.get("degree", len(self.adjacency.get(node, []))),
                }
            )
        return rows

    def shortest_path(
        self,
        source: str,
        target: str,
        max_search_ms: int | None = None,
        max_expanded_nodes: int | None = None,
    ) -> list[str] | None:
        if source == target:
            return [source]
        if source not in self.adjacency or target not in self.adjacency:
            return None

        deadline = (
            time.perf_counter() + (max_search_ms / 1000)
            if max_search_ms is not None
            else None
        )
        expanded_nodes = 0
        forward_parent: dict[str, str | None] = {source: None}
        backward_parent: dict[str, str | None] = {target: None}
        forward_queue: deque[str] = deque([source])
        backward_queue: deque[str] = deque([target])

        while forward_queue and backward_queue:
            if len(forward_queue) <= len(backward_queue):
                meeting, expanded_nodes = self._expand_frontier(
                    forward_queue,
                    forward_parent,
                    backward_parent,
                    deadline,
                    expanded_nodes,
                    max_expanded_nodes,
                    max_search_ms,
                )
            else:
                meeting, expanded_nodes = self._expand_frontier(
                    backward_queue,
                    backward_parent,
                    forward_parent,
                    deadline,
                    expanded_nodes,
                    max_expanded_nodes,
                    max_search_ms,
                )
            if meeting is not None:
                return self._build_path(meeting, forward_parent, backward_parent)
        return None

    def _expand_frontier(
        self,
        queue: deque[str],
        this_parent: dict[str, str | None],
        other_parent: dict[str, str | None],
        deadline: float | None = None,
        expanded_nodes: int = 0,
        max_expanded_nodes: int | None = None,
        max_search_ms: int | None = None,
    ) -> tuple[str | None, int]:
        for _ in range(len(queue)):
            if deadline is not None and time.perf_counter() > deadline:
                raise SearchLimitExceeded(
                    "Search exceeded configured time limit.",
                    expanded_nodes,
                    max_expanded_nodes,
                    max_search_ms,
                )
            if max_expanded_nodes is not None and expanded_nodes >= max_expanded_nodes:
                raise SearchLimitExceeded(
                    "Search exceeded configured expanded-node limit.",
                    expanded_nodes,
                    max_expanded_nodes,
                    max_search_ms,
                )
            node = queue.popleft()
            expanded_nodes += 1
            for index, neighbor in enumerate(self.adjacency.get(node, []), 1):
                if (
                    index % 1000 == 0
                    and deadline is not None
                    and time.perf_counter() > deadline
                ):
                    raise SearchLimitExceeded(
                        "Search exceeded configured time limit.",
                        expanded_nodes,
                        max_expanded_nodes,
                        max_search_ms,
                    )
                if neighbor in this_parent:
                    continue
                this_parent[neighbor] = node
                if neighbor in other_parent:
                    return neighbor, expanded_nodes
                queue.append(neighbor)
        return None, expanded_nodes

    def _build_path(
        self,
        meeting: str,
        forward_parent: dict[str, str | None],
        backward_parent: dict[str, str | None],
    ) -> list[str]:
        left = []
        node: str | None = meeting
        while node is not None:
            left.append(node)
            node = forward_parent[node]
        left.reverse()

        right = []
        node = backward_parent[meeting]
        while node is not None:
            right.append(node)
            node = backward_parent[node]
        return left + right

    def ensure_connectors(self, nodes: list[str]) -> None:
        wanted = {
            node
            for node in nodes
            if not node.startswith(ARTIST_PREFIX) and node not in self.connectors
        }
        if not wanted:
            return
        self.connectors.update(
            artifacts.load_jsonl_by_node(
                self.graph_dir / artifacts.CONNECTORS_FILE, wanted_nodes=wanted
            )
        )

    def collapsed_steps(self, path: list[str]) -> list[dict[str, Any]]:
        self.ensure_connectors(path)
        steps = []
        for index in range(0, len(path) - 2, 2):
            from_node = path[index]
            via_node = path[index + 1]
            to_node = path[index + 2]
            if not from_node.startswith(ARTIST_PREFIX) or not to_node.startswith(ARTIST_PREFIX):
                continue
            steps.append(
                {
                    "from": self.artists.get(from_node, {"node": from_node}),
                    "via": self.connectors.get(via_node, {"node": via_node}),
                    "to": self.artists.get(to_node, {"node": to_node}),
                }
            )
        return steps

    def display_hops(self, path: list[str]) -> int:
        return len(self.collapsed_steps(path))

    def node_label(self, node: str) -> str:
        if node.startswith(ARTIST_PREFIX):
            artist = self.artists.get(node)
            if artist:
                return f"artist: {artist.get('name')} [{artist.get('gid')}]"
            return f"artist: {node}"
        connector = self.connectors.get(node)
        if not connector:
            return f"connector: {node}"
        if connector.get("type") == "recording":
            return f"recording: {connector.get('title')} [{connector.get('gid')}]"
        return f"membership: {connector.get('label')} [link {connector.get('link_id')}]"

    def connector_label(self, connector: dict[str, Any]) -> str:
        if connector.get("type") == "recording":
            return f"recording: {connector.get('title')}"
        if connector.get("type") == "membership":
            return f"membership: {connector.get('label')}"
        return f"connector: {connector.get('node')}"

    # Make a payload suitable for JSON serialization, including the raw path and the collapsed path
    def path_payload(
        self, source: str, target: str, path: list[str], preserve_direction: bool = True
    ) -> dict[str, Any]:
        if preserve_direction and path and path[0] != source and path[-1] == source:
            path = list(reversed(path))
        self.ensure_connectors(path)
        steps = self.collapsed_steps(path)
        return {
            "source": self.artists.get(source, {"node": source}),
            "target": self.artists.get(target, {"node": target}),
            "hop_count": len(steps),
            "raw_path": [
                {
                    "node": node,
                    "kind": "artist" if node.startswith(ARTIST_PREFIX) else "connector",
                    "label": self.node_label(node),
                    "data": self.artists.get(node)
                    if node.startswith(ARTIST_PREFIX)
                    else self.connectors.get(node),
                }
                for node in path
            ],
            "collapsed_path": [
                {
                    "from": step["from"],
                    "via": step["via"],
                    "to": step["to"],
                    "explanation": self.connector_label(step["via"]),
                }
                for step in steps
            ],
        }


def artist_id_from_node(node: str) -> int:
    if not node.startswith(ARTIST_PREFIX):
        raise ValueError(f"not an artist node: {node}")
    return int(node[len(ARTIST_PREFIX) :])


def print_lookup_error(graph: Graph, error: ArtistLookupError) -> None:
    print(str(error), file=sys.stderr)
    if isinstance(error, AmbiguousArtistError):
        print("Top candidates:", file=sys.stderr)
        for candidate in error.candidates:
            bits = [
                str(candidate.get("name")),
                f"gid={candidate.get('gid')}",
                f"degree={candidate.get('degree')}",
            ]
            comment = candidate.get("comment")
            if comment:
                bits.append(f"comment={comment!r}")
            print("  - " + " | ".join(bits), file=sys.stderr)


def print_path(graph: Graph, source: str, target: str, path: list[str] | None) -> None:
    print(f"Resolved source: {graph.node_label(source)}")
    print(f"Resolved target: {graph.node_label(target)}")
    if path is None:
        print("No path found")
        return

    graph.ensure_connectors(path)
    steps = graph.collapsed_steps(path)
    print(f"MCN hops: {len(steps)}")
    print("Raw bipartite path:")
    print("  " + " -> ".join(graph.node_label(node) for node in path))
    print("Collapsed artist path:")
    if not steps:
        print("  same artist")
        return
    for index, step in enumerate(steps, 1):
        print(
            f"  {index}. {step['from'].get('name')} --"
            f"[{graph.connector_label(step['via'])}]--> {step['to'].get('name')}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search a Music Collaboration Number graph")
    parser.add_argument("--graph", required=True, help="Graph artifact directory")
    parser.add_argument("--source", required=True, help="Source artist name or MBID")
    parser.add_argument("--target", required=True, help="Target artist name or MBID")
    args = parser.parse_args(argv)

    graph = Graph.load(args.graph)
    try:
        source = graph.find_artist(args.source)
        target = graph.find_artist(args.target)
    except ArtistLookupError as exc:
        print_lookup_error(graph, exc)
        return 2

    path = graph.shortest_path(source, target)
    print_path(graph, source, target, path)
    return 0 if path is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
