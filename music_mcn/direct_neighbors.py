from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import artifacts
from .search import ArtistLookupError, Graph, normalize_name


def direct_artist_neighbors(graph: Graph, query: str) -> dict[str, Any]:
    node = graph.find_artist(query)
    artist = dict(graph.artists.get(node, {}))

    neighbors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for connector in graph.adjacency.get(node, []):
        for other in graph.adjacency.get(connector, []):
            if other == node or not other.startswith("a:") or other in seen:
                continue
            seen.add(other)
            neighbor = dict(graph.artists.get(other, {}))
            neighbor["node"] = other
            neighbors.append(neighbor)

    neighbors.sort(
        key=lambda item: (
            str(item.get("name", "")).casefold(),
            str(item.get("node", "")),
        )
    )

    return {
        "query": query,
        "resolved": artist,
        "neighbor_count": len(neighbors),
        "neighbors": neighbors,
    }


def write_direct_artist_neighbors(
    graph: Graph,
    query: str,
    output_dir: Path,
) -> Path:
    payload = direct_artist_neighbors(graph, query)
    output_dir.mkdir(parents=True, exist_ok=True)

    resolved = payload.get("resolved", {})
    resolved_node = str(resolved.get("node") or "artist")
    safe_query = normalize_name(query).replace(" ", "-") or "query"
    file_name = f"{safe_query}__{resolved_node}__neighbors.json"
    output_path = output_dir / file_name

    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return output_path


def _load_artists_for_nodes(graph_dir: Path, nodes: set[str]) -> dict[str, dict[str, Any]]:
    return artifacts.load_jsonl_by_node(graph_dir / artifacts.ARTISTS_FILE, nodes)


def _find_artist_from_artifacts(graph_dir: Path, query: str) -> dict[str, Any]:
    name_index = artifacts.read_json(graph_dir / artifacts.NAME_INDEX_FILE)
    by_mbid = name_index.get("by_mbid", {})
    by_name = name_index.get("by_name", {})
    by_alias = name_index.get("by_alias", {})

    stripped = query.strip()
    if not stripped:
        raise ArtistLookupError(f"No artist found for {query!r}")

    mbid_match = by_mbid.get(stripped.casefold())
    if mbid_match:
        artist = _load_artists_for_nodes(graph_dir, {mbid_match}).get(mbid_match)
        if artist is not None:
            return artist

    normalized = normalize_name(stripped)
    candidates = list(by_name.get(normalized, []))
    artists = _load_artists_for_nodes(graph_dir, set(candidates)) if candidates else {}

    def rank(nodes: list[str]) -> list[str]:
        return sorted(
            nodes,
            key=lambda node: (
                -int(artists.get(node, {}).get("degree", 0)),
                str(artists.get(node, {}).get("name", "")).casefold(),
                node,
            ),
        )

    def unique_best(nodes: list[str]) -> str | None:
        if not nodes:
            return None
        ranked = rank(nodes)
        if len(ranked) == 1:
            return ranked[0]
        best_degree = int(artists.get(ranked[0], {}).get("degree", 0))
        second_degree = int(artists.get(ranked[1], {}).get("degree", 0))
        if best_degree > second_degree:
            return ranked[0]
        return None

    if not candidates:
        alias_candidates = list(by_alias.get(normalized, []))
        alias_artists = _load_artists_for_nodes(graph_dir, set(alias_candidates)) if alias_candidates else {}
        if alias_candidates:
            artists = alias_artists
        alias_best = unique_best(alias_candidates)
        if alias_best is not None:
            return artists[alias_best]
        if alias_candidates:
            raise ArtistLookupError(f"Ambiguous artist name {query!r}")
        raise ArtistLookupError(f"No artist found for {query!r}")

    exact = [node for node in candidates if artists.get(node, {}).get("name") == stripped]
    exact_best = unique_best(exact)
    if exact_best is not None:
        return artists[exact_best]
    if exact:
        raise ArtistLookupError(f"Ambiguous artist name {query!r}")

    case_insensitive = [
        node
        for node in candidates
        if str(artists.get(node, {}).get("name", "")).casefold() == stripped.casefold()
    ]
    case_best = unique_best(case_insensitive)
    if case_best is not None:
        return artists[case_best]
    if case_insensitive:
        raise ArtistLookupError(f"Ambiguous artist name {query!r}")

    candidate_best = unique_best(candidates)
    if candidate_best is not None:
        return artists[candidate_best]
    raise ArtistLookupError(f"Ambiguous artist name {query!r}")


def write_direct_artist_neighbors_from_dir(
    graph_dir: Path,
    query: str,
    output_dir: Path,
) -> Path:
    resolved = _find_artist_from_artifacts(graph_dir, query)
    resolved_node = str(resolved.get("node") or "artist")
    resolved_artist_id = int(resolved.get("artist_id"))

    neighbor_nodes: set[str] = set()
    connectors_path = graph_dir / artifacts.CONNECTORS_FILE
    for connector in artifacts.iter_jsonl(connectors_path):
        artist_ids: list[int] = []
        if connector.get("type") == "recording":
            artist_ids = [
                int(item["artist_id"])
                for item in connector.get("credited_artists", [])
                if isinstance(item, dict) and "artist_id" in item
            ]
        elif connector.get("type") == "membership":
            artist_ids = [int(artist_id) for artist_id in connector.get("artist_ids", [])]
        if resolved_artist_id not in artist_ids:
            continue
        for item in connector.get("credited_artists", []):
            if not isinstance(item, dict):
                continue
            artist_id = item.get("artist_id")
            if artist_id != resolved_artist_id:
                neighbor_nodes.add(f"a:{artist_id}")
        for artist_id in connector.get("artist_ids", []):
            if artist_id != resolved_artist_id:
                neighbor_nodes.add(f"a:{artist_id}")

    neighbors = _load_artists_for_nodes(graph_dir, neighbor_nodes) if neighbor_nodes else {}
    payload = {
        "query": query,
        "resolved": resolved,
        "neighbor_count": len(neighbor_nodes),
        "neighbors": [
            {
                **neighbors[node],
                "node": node,
            }
            for node in sorted(
                neighbor_nodes,
                key=lambda node: (
                    str(neighbors.get(node, {}).get("name", "")).casefold(),
                    node,
                ),
            )
            if node in neighbors
        ],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    safe_query = normalize_name(query).replace(" ", "-") or "query"
    file_name = f"{safe_query}__{resolved_node}__neighbors.json"
    output_path = output_dir / file_name
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write the direct artist neighbors for a Music Collaboration Number query",
    )
    parser.add_argument(
        "--graph",
        default="data/artifacts/current",
        help="Graph artifact directory",
    )
    parser.add_argument("--query", required=True, help="Artist name or MBID to resolve")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory to write the JSON report",
    )
    args = parser.parse_args(argv)

    try:
        output_path = write_direct_artist_neighbors_from_dir(
            Path(args.graph),
            args.query,
            Path(args.output_dir),
        )
    except ArtistLookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())