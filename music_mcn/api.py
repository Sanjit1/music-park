from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .cache import PathCache
from .dag import build_canonical_dag, build_shortest_dag
from .search import (
    AmbiguousArtistError,
    ArtistLookupError,
    ArtistNotFoundError,
    Graph,
    SearchLimitExceeded,
    artist_id_from_node,
)

# Defaults are overridden by environment variables MCN_GRAPH_DIR and MCN_ALLOWED_ORIGINS
DEFAULT_GRAPH_DIR = "data/artifacts/current"
DEFAULT_ALLOWED_ORIGINS = [
    "http://localhost:4321",
    "http://127.0.0.1:4321",
]
LOGGER = logging.getLogger("music_mcn.api")
MAIN_GRAPH_ANCHOR = "Bruce Springsteen"

# Utility functions for reading environment variables with different type conversion and defaults
def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning("invalid_int_env name=%s value=%r default=%s", name, raw, default)
        return default

def translate_env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def allowed_origins() -> list[str]:
    raw = os.environ.get("MCN_ALLOWED_ORIGINS")
    if not raw:
        return DEFAULT_ALLOWED_ORIGINS
    return [origin.strip() for origin in raw.split(",") if origin.strip()]

def graph_version(graph: Graph) -> str:
    value = graph.meta.get("graph_version") or graph.meta.get("musicbrainz_export_version")
    if isinstance(value, str) and value:
        return value
    try:
        return graph.graph_dir.resolve().name
    except OSError:
        return graph.graph_dir.name

# Quick information about an artist for the public API.
def public_artist(artist: dict[str, Any]) -> dict[str, Any]:
    return {
        "node": artist.get("node"),
        "artist_id": artist.get("artist_id"),
        "gid": artist.get("gid"),
        "name": artist.get("name"),
        "sort_name": artist.get("sort_name"),
        "comment": artist.get("comment"),
        "degree": artist.get("degree"),
    }


def create_app(graph_dir: str | Path | None = None, cache_db: str | Path | None = None) -> FastAPI:
    resolved_graph_dir = Path(
        graph_dir or os.environ.get("MCN_GRAPH_DIR", DEFAULT_GRAPH_DIR)
    )
    graph = Graph.load(resolved_graph_dir)
    version = graph_version(graph)
    cache = PathCache(cache_db) # cached graph paths
    max_search_ms = env_int("MCN_MAX_SEARCH_MS", 10000)
    max_expanded_nodes = env_int("MCN_MAX_EXPANDED_NODES", 10_000_000)
    max_concurrent_searches = max(1, env_int("MCN_MAX_CONCURRENT_SEARCHES", 3))
    search_semaphore = threading.BoundedSemaphore(max_concurrent_searches)
    enable_cache_stats = translate_env_bool("MCN_ENABLE_CACHE_STATS", False)
    admin_token = os.environ.get("MCN_ADMIN_TOKEN", "")

    app = FastAPI(title="Music Collaboration Number API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.state.graph = graph
    app.state.graph_version = version
    app.state.cache = cache
    app.state.search_limits = {
        "max_search_ms": max_search_ms,
        "max_expanded_nodes": max_expanded_nodes,
        "max_concurrent_searches": max_concurrent_searches,
    }
    if enable_cache_stats and not admin_token:
        LOGGER.warning("cache_stats_enabled_without_admin_token=true")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "graph_version": version,
            "graph_path": str(resolved_graph_dir),
            "artist_count": graph.meta.get("artist_nodes"),
            "connector_count": graph.meta.get("connector_nodes"),
            "raw_edge_count": graph.meta.get("raw_bipartite_edges"),
            "cache": {
                "status": "ok",
                "db_path": str(cache.db_path),
            },
            "limits": app.state.search_limits,
        }

    @app.get("/artists/search")
    def artists_search(
        q: str = Query(..., min_length=1),
        limit: int = Query(10, ge=1, le=50),
    ) -> dict[str, Any]:
        return {
            "query": q,
            "candidates": graph.search_artist_candidates(q, limit=limit),
        }

    @app.get("/mcn/path")
    def mcn_path(source: str, target: str) -> dict[str, Any]:
        started = time.perf_counter()
        acquired = search_semaphore.acquire(blocking=False)
        if not acquired:
            LOGGER.info(
                "mcn_path error=server_busy source_query=%r target_query=%r",
                source,
                target,
            )
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "server_busy",
                    "message": "Too many searches are running. Try again shortly.",
                },
            )
        try:
            try:
                source_node = graph.find_artist(source)
                target_node = graph.find_artist(target)
            except ArtistNotFoundError as exc:
                _log_path_error(started, source, target, "artist_not_found")
                raise HTTPException(
                    status_code=404,
                    detail={"error": "artist_not_found", "message": str(exc)},
                ) from exc
            except AmbiguousArtistError as exc:
                _log_path_error(started, source, target, "ambiguous_artist")
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "ambiguous_artist",
                        "message": str(exc),
                        "candidates": exc.candidates,
                    },
                ) from exc
            except ArtistLookupError as exc:
                _log_path_error(started, source, target, "artist_lookup_error")
                raise HTTPException(
                    status_code=400,
                    detail={"error": "artist_lookup_error", "message": str(exc)},
                ) from exc

            source_id = artist_id_from_node(source_node)
            target_id = artist_id_from_node(target_node)
            cached = cache.get_path(version, source_id, target_id)
            if cached is not None:
                cached["graph_version"] = version
                cached["cache"] = "hit"
                cached["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
                _log_path_success(started, source, target, graph, source_node, target_node, cached)
                return cached

            try:
                path = graph.shortest_path(
                    source_node,
                    target_node,
                    max_search_ms=max_search_ms,
                    max_expanded_nodes=max_expanded_nodes,
                )
            except SearchLimitExceeded as exc:
                _log_path_error(
                    started,
                    source,
                    target,
                    "search_limit_exceeded",
                    graph,
                    source_node,
                    target_node,
                )
                raise HTTPException(
                    status_code=504,
                    detail={
                        "error": "search_limit_exceeded",
                        "message": "Search exceeded configured limits.",
                        "limits": {
                            "max_search_ms": max_search_ms,
                            "max_expanded_nodes": max_expanded_nodes,
                            "expanded_nodes": exc.expanded_nodes,
                        },
                    },
                ) from exc
            if path is None:
                _log_path_error(
                    started,
                    source,
                    target,
                    "no_path_found",
                    graph,
                    source_node,
                    target_node,
                )
                raise _no_path_exception(
                    graph,
                    source_node,
                    target_node,
                    max_search_ms,
                    max_expanded_nodes,
                )

            payload = graph.path_payload(source_node, target_node, path)
            cache.store_path(version, source_id, target_id, payload)
            cache.record_subpaths(version, payload)
            payload["graph_version"] = version
            payload["cache"] = "miss"
            payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            _log_path_success(started, source, target, graph, source_node, target_node, payload)
            return payload
        finally:
            search_semaphore.release()

    @app.get("/mcn/shortest-dag")
    def mcn_shortest_dag(
        source: str,
        target: str,
        limit_per_layer: int = Query(10, ge=1, le=50),
        max_total_nodes: int = Query(150, ge=1, le=500),
        include_debug: bool = Query(False),
    ) -> dict[str, Any]:
        started = time.perf_counter()
        acquired = search_semaphore.acquire(blocking=False)
        if not acquired:
            LOGGER.info(
                "mcn_shortest_dag error=server_busy source_query=%r target_query=%r",
                source,
                target,
            )
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "server_busy",
                    "message": "Too many searches are running. Try again shortly.",
                },
            )
        try:
            try:
                source_node = graph.find_artist(source)
                target_node = graph.find_artist(target)
            except ArtistNotFoundError as exc:
                _log_path_error(
                    started,
                    source,
                    target,
                    "artist_not_found",
                    event="mcn_shortest_dag",
                )
                raise HTTPException(
                    status_code=404,
                    detail={"error": "artist_not_found", "message": str(exc)},
                ) from exc
            except AmbiguousArtistError as exc:
                _log_path_error(
                    started,
                    source,
                    target,
                    "ambiguous_artist",
                    event="mcn_shortest_dag",
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "ambiguous_artist",
                        "message": str(exc),
                        "candidates": exc.candidates,
                    },
                ) from exc
            except ArtistLookupError as exc:
                _log_path_error(
                    started,
                    source,
                    target,
                    "artist_lookup_error",
                    event="mcn_shortest_dag",
                )
                raise HTTPException(
                    status_code=400,
                    detail={"error": "artist_lookup_error", "message": str(exc)},
                ) from exc

            try:
                canonical_path = graph.shortest_path(
                    source_node,
                    target_node,
                    max_search_ms=max_search_ms,
                    max_expanded_nodes=max_expanded_nodes,
                )
            except SearchLimitExceeded as exc:
                _log_path_error(
                    started,
                    source,
                    target,
                    "search_limit_exceeded",
                    graph,
                    source_node,
                    target_node,
                    event="mcn_shortest_dag",
                )
                raise HTTPException(
                    status_code=504,
                    detail={
                        "error": "search_limit_exceeded",
                        "message": "Search exceeded configured limits.",
                        "limits": {
                            "max_search_ms": max_search_ms,
                            "max_expanded_nodes": max_expanded_nodes,
                            "expanded_nodes": exc.expanded_nodes,
                        },
                    },
                ) from exc

            if canonical_path is None:
                _log_path_error(
                    started,
                    source,
                    target,
                    "no_path_found",
                    graph,
                    source_node,
                    target_node,
                    event="mcn_shortest_dag",
                )
                raise _no_path_exception(
                    graph,
                    source_node,
                    target_node,
                    max_search_ms,
                    max_expanded_nodes,
                )

            try:
                payload = build_shortest_dag(
                    graph,
                    source_node,
                    target_node,
                    canonical_path=canonical_path,
                    limit_per_layer=limit_per_layer,
                    max_total_nodes=max_total_nodes,
                    include_debug=include_debug,
                    max_search_ms=max_search_ms,
                    max_expanded_nodes=max_expanded_nodes,
                    started=started,
                )
            except SearchLimitExceeded as exc:
                LOGGER.info(
                    "mcn_shortest_dag partial=canonical_only reason=dag_search_limit_exceeded "
                    "source_query=%r target_query=%r expanded_nodes=%r",
                    source,
                    target,
                    exc.expanded_nodes,
                )
                payload = build_canonical_dag(
                    graph,
                    source_node,
                    target_node,
                    canonical_path,
                    limit_per_layer=limit_per_layer,
                    max_total_nodes=max_total_nodes,
                    started=started,
                    partial_reason="dag_search_limit_exceeded",
                    limit_error=exc,
                )

            if payload is None:
                _log_path_error(
                    started,
                    source,
                    target,
                    "no_path_found",
                    graph,
                    source_node,
                    target_node,
                    event="mcn_shortest_dag",
                )
                raise _no_path_exception(
                    graph,
                    source_node,
                    target_node,
                    max_search_ms,
                    max_expanded_nodes,
                )

            payload["graph_version"] = version
            payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            _log_path_success(
                started,
                source,
                target,
                graph,
                source_node,
                target_node,
                payload,
                event="mcn_shortest_dag",
            )
            return payload
        finally:
            search_semaphore.release()

    @app.get("/cache/stats")
    def cache_stats(
        limit: int = Query(10, ge=1, le=50),
        x_mcn_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not enable_cache_stats:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        if admin_token and x_mcn_admin_token != admin_token:
            raise HTTPException(status_code=403, detail={"error": "forbidden"})
        return {
            "graph_version": version,
            **cache.stats(version, limit=limit),
        }

    return app


def _no_path_exception(
    graph: Graph,
    source_node: str,
    target_node: str,
    max_search_ms: int | None,
    max_expanded_nodes: int | None,
) -> HTTPException:
    try:
        return HTTPException(
            status_code=404,
            detail=_no_path_detail(
                graph,
                source_node,
                target_node,
                max_search_ms,
                max_expanded_nodes,
            ),
        )
    except SearchLimitExceeded as exc:
        return HTTPException(
            status_code=504,
            detail={
                "error": "search_limit_exceeded",
                "message": "Search exceeded configured limits.",
                "limits": {
                    "max_search_ms": max_search_ms,
                    "max_expanded_nodes": max_expanded_nodes,
                    "expanded_nodes": exc.expanded_nodes,
                },
            },
        )


def _no_path_detail(
    graph: Graph,
    source_node: str,
    target_node: str,
    max_search_ms: int | None,
    max_expanded_nodes: int | None,
) -> dict[str, Any]:
    source = public_artist(graph.artists[source_node])
    target = public_artist(graph.artists[target_node])
    detail = {
        "error": "no_path_found",
        "source": source,
        "target": target,
    }
    connectivity = _main_graph_connectivity(
        graph,
        source_node,
        target_node,
        max_search_ms,
        max_expanded_nodes,
    )
    if connectivity is not None:
        detail["main_graph"] = connectivity
        detail["message"] = _main_graph_message(source, target, connectivity)
    return detail


def _main_graph_connectivity(
    graph: Graph,
    source_node: str,
    target_node: str,
    max_search_ms: int | None,
    max_expanded_nodes: int | None,
) -> dict[str, Any] | None:
    try:
        anchor_node = graph.find_artist(MAIN_GRAPH_ANCHOR)
    except ArtistLookupError:
        return None

    source_connected = graph.is_connected(
        source_node,
        anchor_node,
        max_search_ms=max_search_ms,
        max_expanded_nodes=max_expanded_nodes,
    )
    # A no-path result means both artists cannot be in Bruce's component.
    target_connected = False
    if not source_connected:
        target_connected = graph.is_connected(
            target_node,
            anchor_node,
            max_search_ms=max_search_ms,
            max_expanded_nodes=max_expanded_nodes,
        )

    disconnected = []
    if not source_connected:
        disconnected.append("source")
    if not target_connected:
        disconnected.append("target")
    return {
        "anchor": MAIN_GRAPH_ANCHOR,
        "source_connected": source_connected,
        "target_connected": target_connected,
        "disconnected": disconnected,
    }


def _main_graph_message(
    source: dict[str, Any],
    target: dict[str, Any],
    connectivity: dict[str, Any],
) -> str:
    disconnected_names = []
    if not connectivity["source_connected"]:
        disconnected_names.append(str(source.get("name") or "Source"))
    if not connectivity["target_connected"]:
        disconnected_names.append(str(target.get("name") or "Target"))
    if len(disconnected_names) == 1:
        return f"{disconnected_names[0]} is not connected to the main graph."
    if len(disconnected_names) == 2:
        return (
            f"{disconnected_names[0]} and {disconnected_names[1]} are not connected "
            "to the main graph."
        )
    return "No path found."


# ? ------------------------------------------- Log stuff
def _artist_log_fields(graph: Graph, node: str, prefix: str) -> dict[str, Any]:
    artist = graph.artists.get(node, {})
    return {
        f"{prefix}_artist_id": artist.get("artist_id"),
        f"{prefix}_artist_name": artist.get("name"),
    }

def _log_path_success(
    started: float,
    source_query: str,
    target_query: str,
    graph: Graph,
    source_node: str,
    target_node: str,
    payload: dict[str, Any],
    event: str = "mcn_path",
) -> None:
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    fields = {
        "source_query": source_query,
        "target_query": target_query,
        "cache": payload.get("cache"),
        "hop_count": payload.get("hop_count", payload.get("mcn_hops")),
        "elapsed_ms": elapsed_ms,
        **_artist_log_fields(graph, source_node, "source"),
        **_artist_log_fields(graph, target_node, "target"),
    }
    LOGGER.info("%s %s", event, " ".join(f"{key}={value!r}" for key, value in fields.items()))

def _log_path_error(
    started: float,
    source_query: str,
    target_query: str,
    error: str,
    graph: Graph | None = None,
    source_node: str | None = None,
    target_node: str | None = None,
    event: str = "mcn_path",
) -> None:
    fields: dict[str, Any] = {
        "source_query": source_query,
        "target_query": target_query,
        "error": error,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    if graph is not None and source_node is not None:
        fields.update(_artist_log_fields(graph, source_node, "source"))
    if graph is not None and target_node is not None:
        fields.update(_artist_log_fields(graph, target_node, "target"))
    LOGGER.info("%s %s", event, " ".join(f"{key}={value!r}" for key, value in fields.items()))
# ? -------------------------------------------


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("MCN_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Run the Music Collaboration Number API")
    parser.add_argument(
        "--graph",
        default=os.environ.get("MCN_GRAPH_DIR"),
        help="Graph artifact directory",
    )
    parser.add_argument(
        "--cache-db",
        default=os.environ.get("MCN_CACHE_DB"),
        help="SQLite cache path",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCN_HOST", "127.0.0.1"),
        help="Bind host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCN_PORT", "8000")),
        help="Bind port",
    )
    args = parser.parse_args(argv)

    app = create_app(args.graph, args.cache_db)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
