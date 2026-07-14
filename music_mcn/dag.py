from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from typing import Any

from . import artifacts
from .search import (
    ARTIST_PREFIX,
    MEMBERSHIP_PREFIX,
    RECORDING_PREFIX,
    Graph,
    SearchLimitExceeded,
    artist_id_from_node,
)


DEFAULT_COUNT_CAP = 1_000_000_000
COUNT_CAP_ENV = "MCN_DAG_COUNT_CAP"


def build_shortest_dag(
    graph: Graph,
    source: str,
    target: str,
    canonical_path: list[str] | None = None,
    limit_per_layer: int = 10,
    max_total_nodes: int = 150,
    include_debug: bool = False,
    max_search_ms: int | None = None,
    max_expanded_nodes: int | None = None,
    started: float | None = None,
) -> dict[str, Any] | None:
    if limit_per_layer < 1:
        raise ValueError("limit_per_layer must be at least 1")
    if max_total_nodes < 1:
        raise ValueError("max_total_nodes must be at least 1")

    search_started = time.perf_counter() if started is None else started
    count_cap = _count_cap_from_env()
    if canonical_path is None:
        canonical_path = graph.shortest_path(
            source,
            target,
            max_search_ms=max_search_ms,
            max_expanded_nodes=max_expanded_nodes,
        )
    if canonical_path is None:
        return None
    if canonical_path and canonical_path[0] == target and canonical_path[-1] == source:
        canonical_path = list(reversed(canonical_path))

    raw_distance = max(0, len(canonical_path) - 1)
    deadline = (
        search_started + (max_search_ms / 1000)
        if max_search_ms is not None
        else None
    )

    expanded_nodes = 0
    dist_s, expanded_nodes = _bounded_bfs_distances(
        graph,
        source,
        raw_distance,
        deadline,
        expanded_nodes,
        max_expanded_nodes,
        max_search_ms,
    )
    dist_t, expanded_nodes = _bounded_bfs_distances(
        graph,
        target,
        raw_distance,
        deadline,
        expanded_nodes,
        max_expanded_nodes,
        max_search_ms,
    )

    dag_nodes = {
        node
        for node, source_distance in dist_s.items()
        if source_distance + dist_t.get(node, raw_distance + 1) == raw_distance
    }
    layers_by_index: dict[int, list[str]] = defaultdict(list)
    for node in dag_nodes:
        layers_by_index[dist_s[node]].append(node)

    dag_edges = _shortest_dag_edges(
        graph,
        dag_nodes,
        dist_s,
        dist_t,
        raw_distance,
        deadline,
        expanded_nodes,
        max_expanded_nodes,
        max_search_ms,
    )
    out_edges: dict[str, list[str]] = defaultdict(list)
    in_edges: dict[str, list[str]] = defaultdict(list)
    for left, right in dag_edges:
        out_edges[left].append(right)
        in_edges[right].append(left)

    prefix_count = _prefix_counts(
        source,
        raw_distance,
        layers_by_index,
        out_edges,
        count_cap,
    )
    suffix_count = _suffix_counts(
        target,
        raw_distance,
        layers_by_index,
        out_edges,
        count_cap,
    )
    through_scores = {
        node: min(
            count_cap,
            prefix_count.get(node, 0) * suffix_count.get(node, 0),
        )
        for node in dag_nodes
    }

    forced_nodes = set(canonical_path)
    selected_nodes = _select_nodes(
        graph,
        raw_distance,
        layers_by_index,
        forced_nodes,
        through_scores,
        limit_per_layer,
        max_total_nodes,
    )
    selected_edges = [
        (left, right)
        for left, right in dag_edges
        if left in selected_nodes and right in selected_nodes
    ]
    selected_nodes, selected_edges = _prune_selected_subgraph(
        source,
        target,
        forced_nodes,
        selected_nodes,
        selected_edges,
    )

    canonical_payload = graph.path_payload(source, target, canonical_path)
    connector_metadata_truncated = _ensure_extra_connectors_best_effort(
        graph,
        sorted(
            selected_nodes - forced_nodes,
            key=lambda node: (dist_s.get(node, raw_distance + 1), node),
        ),
        deadline,
    )

    visual_ids = {node: _visual_id(node) for node in selected_nodes}
    layer_payloads = []
    returned_nodes = 0
    for index in range(raw_distance + 1):
        ranked = sorted(
            (node for node in selected_nodes if dist_s.get(node) == index),
            key=lambda node: _rank_key(graph, node, through_scores),
        )
        returned_nodes += len(ranked)
        layer_payloads.append(
            {
                "index": index,
                "nodes": [
                    _format_layer_node(graph, node, through_scores.get(node, 0))
                    for node in ranked
                ],
            }
        )

    edge_payloads = [
        {"source": visual_ids[left], "target": visual_ids[right]}
        for left, right in selected_edges
    ]
    truncated = (
        len(dag_nodes) > returned_nodes
        or len(dag_edges) > len(edge_payloads)
    )

    stats: dict[str, Any] = {
        "full_dag_nodes_before_cap": len(dag_nodes),
        "full_dag_edges_before_cap": len(dag_edges),
        "returned_nodes": returned_nodes,
        "returned_edges": len(edge_payloads),
        "elapsed_ms": round((time.perf_counter() - search_started) * 1000, 2),
        "count_cap": count_cap,
    }
    if connector_metadata_truncated:
        stats["connector_metadata_truncated"] = True
    if include_debug:
        stats.update(
            {
                "expanded_nodes": expanded_nodes,
                "source_reachable_nodes": len(dist_s),
                "target_reachable_nodes": len(dist_t),
                "layer_widths_before_cap": {
                    str(index): len(layers_by_index.get(index, []))
                    for index in range(raw_distance + 1)
                },
            }
        )

    return {
        "source": _format_artist_ref(graph, source),
        "target": _format_artist_ref(graph, target),
        "raw_distance": raw_distance,
        "mcn_hops": canonical_payload.get("hop_count", raw_distance // 2),
        "limit_per_layer": limit_per_layer,
        "max_total_nodes": max_total_nodes,
        "truncated": truncated,
        "cache": "miss",
        "layers": layer_payloads,
        "edges": edge_payloads,
        "canonical_path": {
            "hop_count": canonical_payload.get("hop_count", raw_distance // 2),
            "raw_path": canonical_payload.get("raw_path", []),
            "collapsed_path": canonical_payload.get("collapsed_path", []),
        },
        "stats": stats,
    }


def build_canonical_dag(
    graph: Graph,
    source: str,
    target: str,
    canonical_path: list[str],
    limit_per_layer: int = 10,
    max_total_nodes: int = 150,
    started: float | None = None,
    partial_reason: str | None = None,
    limit_error: SearchLimitExceeded | None = None,
) -> dict[str, Any]:
    search_started = time.perf_counter() if started is None else started
    count_cap = _count_cap_from_env()
    if canonical_path and canonical_path[0] == target and canonical_path[-1] == source:
        canonical_path = list(reversed(canonical_path))

    raw_distance = max(0, len(canonical_path) - 1)
    canonical_payload = graph.path_payload(source, target, canonical_path)
    visual_ids = {node: _visual_id(node) for node in canonical_path}
    layer_payloads = [
        {
            "index": index,
            "nodes": [_format_layer_node(graph, node, 1)],
        }
        for index, node in enumerate(canonical_path)
    ]
    edge_payloads = [
        {
            "source": visual_ids[canonical_path[index]],
            "target": visual_ids[canonical_path[index + 1]],
        }
        for index in range(len(canonical_path) - 1)
    ]
    stats: dict[str, Any] = {
        "full_dag_nodes_before_cap": None,
        "full_dag_edges_before_cap": None,
        "returned_nodes": len(canonical_path),
        "returned_edges": len(edge_payloads),
        "elapsed_ms": round((time.perf_counter() - search_started) * 1000, 2),
        "count_cap": count_cap,
    }
    if partial_reason is not None:
        stats["partial_reason"] = partial_reason
    if limit_error is not None:
        stats["expanded_nodes"] = limit_error.expanded_nodes

    return {
        "source": _format_artist_ref(graph, source),
        "target": _format_artist_ref(graph, target),
        "raw_distance": raw_distance,
        "mcn_hops": canonical_payload.get("hop_count", raw_distance // 2),
        "limit_per_layer": limit_per_layer,
        "max_total_nodes": max_total_nodes,
        "truncated": partial_reason is not None,
        "cache": "miss",
        "layers": layer_payloads,
        "edges": edge_payloads,
        "canonical_path": {
            "hop_count": canonical_payload.get("hop_count", raw_distance // 2),
            "raw_path": canonical_payload.get("raw_path", []),
            "collapsed_path": canonical_payload.get("collapsed_path", []),
        },
        "stats": stats,
    }


def _bounded_bfs_distances(
    graph: Graph,
    start: str,
    max_depth: int,
    deadline: float | None,
    expanded_nodes: int,
    max_expanded_nodes: int | None,
    max_search_ms: int | None,
) -> tuple[dict[str, int], int]:
    distances = {start: 0}
    queue: deque[str] = deque([start])
    while queue:
        _check_limits(deadline, expanded_nodes, max_expanded_nodes, max_search_ms)
        node = queue.popleft()
        expanded_nodes += 1
        depth = distances[node]
        if depth >= max_depth:
            continue
        for index, neighbor in enumerate(graph.adjacency.get(node, []), 1):
            if index % 1000 == 0:
                _check_limits(deadline, expanded_nodes, max_expanded_nodes, max_search_ms)
            if neighbor in distances:
                continue
            distances[neighbor] = depth + 1
            queue.append(neighbor)
    return distances, expanded_nodes


def _count_cap_from_env() -> int:
    raw = os.environ.get(COUNT_CAP_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_COUNT_CAP
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_COUNT_CAP


def _check_limits(
    deadline: float | None,
    expanded_nodes: int,
    max_expanded_nodes: int | None,
    max_search_ms: int | None,
) -> None:
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


def _ensure_extra_connectors_best_effort(
    graph: Graph,
    nodes: list[str],
    deadline: float | None,
) -> bool:
    wanted = {
        node
        for node in nodes
        if not node.startswith(ARTIST_PREFIX) and node not in graph.connectors
    }
    if not wanted:
        return False
    if deadline is None:
        graph.ensure_connectors(list(wanted))
        return False
    if time.perf_counter() > deadline:
        return True

    connectors_path = graph.graph_dir / artifacts.CONNECTORS_FILE
    with connectors_path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle, 1):
            node = artifacts.jsonl_node_value(line)
            if node in wanted:
                graph.connectors[node] = json.loads(line)
                wanted.remove(node)
                if not wanted:
                    return False
            if index % 1000 == 0 and time.perf_counter() > deadline:
                return True
    return bool(wanted)


def _shortest_dag_edges(
    graph: Graph,
    dag_nodes: set[str],
    dist_s: dict[str, int],
    dist_t: dict[str, int],
    raw_distance: int,
    deadline: float | None,
    expanded_nodes: int,
    max_expanded_nodes: int | None,
    max_search_ms: int | None,
) -> list[tuple[str, str]]:
    edge_set: set[tuple[str, str]] = set()
    for left in dag_nodes:
        _check_limits(deadline, expanded_nodes, max_expanded_nodes, max_search_ms)
        left_distance = dist_s[left]
        if left_distance >= raw_distance:
            continue
        for index, right in enumerate(graph.adjacency.get(left, []), 1):
            if index % 1000 == 0:
                _check_limits(deadline, expanded_nodes, max_expanded_nodes, max_search_ms)
            if right not in dag_nodes:
                continue
            right_distance = dist_s.get(right)
            if right_distance != left_distance + 1:
                continue
            if left_distance + 1 + dist_t.get(right, raw_distance + 1) != raw_distance:
                continue
            edge_set.add((left, right))
    return sorted(edge_set, key=lambda edge: (dist_s[edge[0]], edge[0], edge[1]))


def _prefix_counts(
    source: str,
    raw_distance: int,
    layers_by_index: dict[int, list[str]],
    out_edges: dict[str, list[str]],
    count_cap: int,
) -> dict[str, int]:
    counts = {source: 1}
    for index in range(raw_distance):
        for node in sorted(layers_by_index.get(index, [])):
            node_count = counts.get(node, 0)
            if node_count == 0:
                continue
            for neighbor in out_edges.get(node, []):
                counts[neighbor] = min(
                    count_cap,
                    counts.get(neighbor, 0) + node_count,
                )
    return counts


def _suffix_counts(
    target: str,
    raw_distance: int,
    layers_by_index: dict[int, list[str]],
    out_edges: dict[str, list[str]],
    count_cap: int,
) -> dict[str, int]:
    counts = {target: 1}
    for index in range(raw_distance - 1, -1, -1):
        for node in sorted(layers_by_index.get(index, [])):
            total = 0
            for neighbor in out_edges.get(node, []):
                total = min(count_cap, total + counts.get(neighbor, 0))
            if total:
                counts[node] = total
    return counts


def _select_nodes(
    graph: Graph,
    raw_distance: int,
    layers_by_index: dict[int, list[str]],
    forced_nodes: set[str],
    through_scores: dict[str, int],
    limit_per_layer: int,
    max_total_nodes: int,
) -> set[str]:
    selected: set[str] = set()
    selected_by_layer: dict[int, set[str]] = {}
    for index in range(raw_distance + 1):
        forced = {
            node
            for node in layers_by_index.get(index, [])
            if node in forced_nodes
        }
        selected_by_layer[index] = set(forced)
        selected.update(forced)

    extra_budget = max(0, max_total_nodes - len(selected))
    for index in range(raw_distance + 1):
        layer_selected = selected_by_layer[index]
        slots = max(0, limit_per_layer - len(layer_selected))
        if slots == 0 or extra_budget == 0:
            continue
        slots = min(slots, extra_budget)
        candidates = sorted(
            (
                node
                for node in layers_by_index.get(index, [])
                if node not in layer_selected
            ),
            key=lambda node: _rank_key(graph, node, through_scores),
        )
        for node in candidates[:slots]:
            layer_selected.add(node)
            selected.add(node)
            extra_budget -= 1
    return selected


def _prune_selected_subgraph(
    source: str,
    target: str,
    forced_nodes: set[str],
    selected_nodes: set[str],
    selected_edges: list[tuple[str, str]],
) -> tuple[set[str], list[tuple[str, str]]]:
    out_edges: dict[str, list[str]] = defaultdict(list)
    in_edges: dict[str, list[str]] = defaultdict(list)
    for left, right in selected_edges:
        out_edges[left].append(right)
        in_edges[right].append(left)

    reachable = _walk_selected(source, out_edges)
    can_reach_target = _walk_selected(target, in_edges)
    keep = forced_nodes | {
        node
        for node in selected_nodes
        if node in reachable and node in can_reach_target
    }
    pruned_edges = [
        (left, right)
        for left, right in selected_edges
        if left in keep and right in keep
    ]
    return keep, pruned_edges


def _walk_selected(start: str, edges: dict[str, list[str]]) -> set[str]:
    seen = {start}
    queue: deque[str] = deque([start])
    while queue:
        node = queue.popleft()
        for neighbor in edges.get(node, []):
            if neighbor in seen:
                continue
            seen.add(neighbor)
            queue.append(neighbor)
    return seen


def _rank_key(
    graph: Graph,
    node: str,
    through_scores: dict[str, int],
) -> tuple[int, int, str]:
    return (-through_scores.get(node, 0), len(graph.adjacency.get(node, [])), node)


def _visual_id(node: str) -> str:
    if node.startswith(ARTIST_PREFIX):
        return f"artist:{node[len(ARTIST_PREFIX):]}"
    if node.startswith(RECORDING_PREFIX):
        return f"recording:{node[len(RECORDING_PREFIX):]}"
    if node.startswith(MEMBERSHIP_PREFIX):
        return f"membership:{node[len(MEMBERSHIP_PREFIX):]}"
    return f"node:{node}"


def _numeric_id(node: str) -> int | str:
    for prefix in (ARTIST_PREFIX, RECORDING_PREFIX, MEMBERSHIP_PREFIX):
        if node.startswith(prefix):
            raw = node[len(prefix) :]
            try:
                return int(raw)
            except ValueError:
                return raw
    return node


def _format_artist_ref(graph: Graph, node: str) -> dict[str, Any]:
    artist = graph.artists.get(node, {"node": node})
    artist_id = artist.get("artist_id")
    gid = artist.get("gid")
    return {
        "id": artist_id,
        "node": artist.get("node", node),
        "artist_id": artist_id,
        "name": artist.get("name"),
        "sort_name": artist.get("sort_name"),
        "comment": artist.get("comment"),
        "gid": gid,
        "mbid": gid,
        "degree": artist.get("degree"),
    }


def _format_layer_node(graph: Graph, node: str, score: int) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": _visual_id(node),
        "node": node,
        "node_id": _numeric_id(node),
        "score": score,
        "degree": len(graph.adjacency.get(node, [])),
    }
    if node.startswith(ARTIST_PREFIX):
        artist = graph.artists.get(node, {"node": node})
        gid = artist.get("gid")
        base.update(
            {
                "type": "artist",
                "artist_id": artist.get("artist_id", artist_id_from_node(node)),
                "name": artist.get("name"),
                "sort_name": artist.get("sort_name"),
                "comment": artist.get("comment"),
                "gid": gid,
                "mbid": gid,
            }
        )
        return base

    connector = graph.connectors.get(node, {"node": node})
    connector_type = connector.get("type") or _connector_type_from_node(node)
    base["type"] = connector_type
    if connector_type == "recording":
        gid = connector.get("gid")
        base.update(
            {
                "recording_id": connector.get("recording_id", _numeric_id(node)),
                "title": connector.get("title"),
                "gid": gid,
                "mbid": gid,
                "artist_credit_id": connector.get("artist_credit_id"),
            }
        )
    elif connector_type == "membership":
        base.update(
            {
                "relationship_id": connector.get("relationship_id", _numeric_id(node)),
                "link_id": connector.get("link_id"),
                "link_type_id": connector.get("link_type_id"),
                "label": connector.get("label"),
                "artist_ids": connector.get("artist_ids"),
            }
        )
    else:
        base["label"] = connector.get("label")
    return base


def _connector_type_from_node(node: str) -> str:
    if node.startswith(RECORDING_PREFIX):
        return "recording"
    if node.startswith(MEMBERSHIP_PREFIX):
        return "membership"
    return "connector"
