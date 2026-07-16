from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from . import __version__
from . import artifacts, mbdump
from .search import normalize_name


ARTIST_PREFIX = "a:"
RECORDING_PREFIX = "r:"
MEMBERSHIP_PREFIX = "m:"


REQUIRED_TABLES = [
    "artist",
    "artist_alias",
    "recording",
    "artist_credit_name",
    "link_type",
    "link",
    "l_artist_artist",
]


artist_node = lambda artist_id: f"{ARTIST_PREFIX}{artist_id}"
recording_node = lambda recording_id: f"{RECORDING_PREFIX}{recording_id}"
membership_node = lambda relationship_id: f"{MEMBERSHIP_PREFIX}{relationship_id}"

def _add_edge(adjacency: dict[str, list[str]], left: str, right: str) -> None:
    adjacency.setdefault(left, []).append(right)
    adjacency.setdefault(right, []).append(left)


def _add_index_value(index: dict[str, list[str]], raw_value: object, node: str) -> str:
    normalized = normalize_name(str(raw_value or ""))
    if normalized:
        index[normalized].append(node)
    return normalized


def _add_search_tokens(by_token: dict[str, set[str]], normalized: str, node: str) -> None:
    for token in normalized.split():
        by_token[token].add(node)


def _ignored_by_name(dump_dir: Path, progress: bool) -> set[int]:
    # Broad placeholder artists make misleading shortcuts through the graph.
    # For example, "Various Artists" can connect any two random artists that appear on a compilation album.
    ignored: set[int] = set()
    for count, artist in enumerate(mbdump.iter_artists(dump_dir), 1):
        if "various" in str(artist["name"]).casefold():
            ignored.add(int(artist["artist_id"]))
        if progress and count % 1_000_000 == 0:
            print(f"artists scanned for ignored names: {count:,}", file=sys.stderr, flush=True)
    return ignored

# ? -------------------------------- Remove artists with over 100k recordings --------------------------------
def _recording_counts_by_credit(dump_dir: Path, progress: bool) -> dict[int, int]:
    # First pass: learn how often each artist_credit appears on recordings.
    counts: dict[int, int] = defaultdict(int)
    for count, recording in enumerate(mbdump.iter_recordings(dump_dir), 1):
        counts[int(recording["artist_credit_id"])] += 1
        if progress and count % 1_000_000 == 0:
            print(f"recordings counted: {count:,}", file=sys.stderr, flush=True)
    return dict(counts)

def _ignored_by_recording_volume(
    dump_dir: Path,
    credit_recording_counts: dict[int, int],
    already_ignored: set[int],
    max_recordings: int,
    progress: bool,
) -> tuple[set[int], dict[str, int]]:
    # Convert artist_credit volume into per-artist volume for filtering.
    artist_counts: dict[int, int] = defaultdict(int)
    credited_rows = 0
    used_credit_rows = 0
    for credited_rows, (credit_id, _position, artist_id, _name) in enumerate(
        mbdump.iter_artist_credit_names(dump_dir), 1
    ):
        if artist_id in already_ignored:
            continue
        recording_count = credit_recording_counts.get(credit_id, 0)
        if recording_count:
            artist_counts[artist_id] += recording_count
            used_credit_rows += 1
        if progress and credited_rows % 1_000_000 == 0:
            print(
                f"artist credit rows counted for artist volume: {credited_rows:,}",
                file=sys.stderr,
                flush=True,
            )

    over_limit = {
        artist_id for artist_id, count in artist_counts.items() if count > max_recordings
    }
    stats = {
        "artist_credit_name_rows": credited_rows,
        "artist_credit_name_rows_used_for_artist_volume": used_credit_rows,
        "artists_over_recording_limit": len(over_limit),
    }
    return over_limit, stats


def _multi_artist_credits(
    dump_dir: Path,
    credit_recording_counts: dict[int, int],
    ignored_artists: set[int],
    progress: bool,
) -> tuple[dict[int, list[tuple[int, str]]], dict[str, int]]:
    # Only multi-artist credits can create recording-based artist links.
    kept_counts: dict[int, int] = defaultdict(int)
    for count, (credit_id, _position, artist_id, _name) in enumerate(
        mbdump.iter_artist_credit_names(dump_dir), 1
    ):
        if artist_id not in ignored_artists and credit_id in credit_recording_counts:
            kept_counts[credit_id] += 1
        if progress and count % 1_000_000 == 0:
            print(
                f"artist credits scanned for multi-artist counts: {count:,}",
                file=sys.stderr,
                flush=True,
            )

    multi_credit_ids = {
        credit_id for credit_id, artist_count in kept_counts.items() if artist_count >= 2
    }
    stats = {
        "artist_credits_with_recordings": len(credit_recording_counts),
        "artist_credits_with_kept_artists": len(kept_counts),
        "multi_artist_credits": len(multi_credit_ids),
    }
    del kept_counts

    credit_artists: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for count, (credit_id, position, artist_id, credited_name) in enumerate(
        mbdump.iter_artist_credit_names(dump_dir), 1
    ):
        if credit_id in multi_credit_ids and artist_id not in ignored_artists:
            credit_artists[credit_id].append((position, artist_id, credited_name))
        if progress and count % 1_000_000 == 0:
            print(f"multi-artist credit rows loaded: {count:,}", file=sys.stderr, flush=True)

    del multi_credit_ids

    clean_credit_artists: dict[int, list[tuple[int, str]]] = {}
    for credit_id, rows in credit_artists.items():
        rows.sort(key=lambda item: item[0])
        seen: set[int] = set()
        artists: list[tuple[int, str]] = []
        for _position, artist_id, credited_name in rows:
            if artist_id in seen:
                continue
            seen.add(artist_id)
            artists.append((artist_id, credited_name))
        if len(artists) >= 2:
            clean_credit_artists[credit_id] = artists

    stats["multi_artist_credits_after_dedupe"] = len(clean_credit_artists)
    return clean_credit_artists, stats

# ? ----------------------------------------------------------------

def _write_recording_connectors(
    dump_dir: Path,
    connectors_handle: TextIO,
    adjacency: dict[str, list[str]],
    credit_artists: dict[int, list[tuple[int, str]]],
    used_artist_ids: set[int],
    progress: bool,
) -> dict[str, int]:
    # Recording connectors form artist -> recording -> artist paths.
    recording_connectors = 0
    recording_edges = 0
    recordings_seen = 0
    for recordings_seen, recording in enumerate(mbdump.iter_recordings(dump_dir), 1):
        artists_for_credit = credit_artists.get(int(recording["artist_credit_id"]))
        if not artists_for_credit:
            if progress and recordings_seen % 1_000_000 == 0:
                print(
                    f"recordings scanned for connectors: {recordings_seen:,}",
                    file=sys.stderr,
                    flush=True,
                )
            continue

        node = recording_node(int(recording["recording_id"]))
        artifacts.write_jsonl_item(
            connectors_handle,
            {
                "node": node,
                "type": "recording",
                "recording_id": recording["recording_id"],
                "gid": recording["gid"],
                "title": recording["title"],
                "artist_credit_id": recording["artist_credit_id"],
                "credited_artists": [
                    {"artist_id": artist_id, "name": credited_name}
                    for artist_id, credited_name in artists_for_credit
                ],
            },
        )
        recording_connectors += 1
        for artist_id, _credited_name in artists_for_credit:
            used_artist_ids.add(artist_id)
            _add_edge(adjacency, artist_node(artist_id), node)
            recording_edges += 1
        if progress and recordings_seen % 1_000_000 == 0:
            print(
                f"recordings scanned for connectors: {recordings_seen:,}",
                file=sys.stderr,
                flush=True,
            )

    return {
        "recordings_seen": recordings_seen,
        "recording_connectors": recording_connectors,
        "recording_raw_edges": recording_edges,
    }


def _write_membership_connectors(
    dump_dir: Path,
    connectors_handle: TextIO,
    adjacency: dict[str, list[str]],
    ignored_artists: set[int],
    used_artist_ids: set[int],
    progress: bool,
) -> dict[str, int | list[int]]:
    # Membership connectors form artist -> membership relationship -> artist paths.
    link_type_names = mbdump.find_member_of_band_link_types(dump_dir)
    if not link_type_names:
        raise RuntimeError("No artist-artist link_type containing 'member of band' found")

    link_id_to_type: dict[int, int] = {}
    for count, (link_id, link_type_id) in enumerate(mbdump.iter_links(dump_dir), 1):
        if link_type_id in link_type_names:
            link_id_to_type[link_id] = link_type_id
        if progress and count % 1_000_000 == 0:
            print(f"links scanned for member-of-band types: {count:,}", file=sys.stderr, flush=True)

    membership_connectors = 0
    membership_edges = 0
    relationships_seen = 0
    for relationships_seen, (relationship_id, link_id, artist0, artist1) in enumerate(
        mbdump.iter_artist_artist_links(dump_dir), 1
    ):
        link_type_id = link_id_to_type.get(link_id)
        if link_type_id is None:
            if progress and relationships_seen % 1_000_000 == 0:
                print(
                    f"artist-artist links scanned for memberships: {relationships_seen:,}",
                    file=sys.stderr,
                    flush=True,
                )
            continue
        if artist0 in ignored_artists or artist1 in ignored_artists or artist0 == artist1:
            if progress and relationships_seen % 1_000_000 == 0:
                print(
                    f"artist-artist links scanned for memberships: {relationships_seen:,}",
                    file=sys.stderr,
                    flush=True,
                )
            continue

        node = membership_node(relationship_id)
        label = link_type_names[link_type_id]
        artifacts.write_jsonl_item(
            connectors_handle,
            {
                "node": node,
                "type": "membership",
                "relationship_id": relationship_id,
                "link_id": link_id,
                "link_type_id": link_type_id,
                "label": label,
                "artist_ids": [artist0, artist1],
            },
        )
        for artist_id in (artist0, artist1):
            used_artist_ids.add(artist_id)
            _add_edge(adjacency, artist_node(artist_id), node)
            membership_edges += 1
        membership_connectors += 1
        if progress and relationships_seen % 1_000_000 == 0:
            print(
                f"artist-artist links scanned for memberships: {relationships_seen:,}",
                file=sys.stderr,
                flush=True,
            )

    return {
        "member_of_band_link_type_ids": sorted(link_type_names),
        "member_of_band_link_ids": len(link_id_to_type),
        "artist_artist_relationships_seen": relationships_seen,
        "membership_connectors": membership_connectors,
        "membership_raw_edges": membership_edges,
    }


def _write_artists_and_index(
    dump_dir: Path,
    out_dir: Path,
    adjacency: dict[str, list[str]],
    used_artist_ids: set[int],
    progress: bool,
) -> dict[str, int]:
    # Write only artists that actually survived into the graph, plus lookup indexes.
    by_name: dict[str, list[str]] = defaultdict(list)
    by_sort_name: dict[str, list[str]] = defaultdict(list)
    by_alias: dict[str, list[str]] = defaultdict(list)
    by_mbid: dict[str, str] = {}
    by_token: dict[str, set[str]] = defaultdict(set)
    artist_meta: dict[str, dict[str, int | str | None]] = {}
    found = 0
    aliases_seen = 0
    aliases_indexed = 0

    with (out_dir / artifacts.ARTISTS_FILE).open("w", encoding="utf-8") as handle:
        for count, artist in enumerate(mbdump.iter_artists(dump_dir), 1):
            artist_id = int(artist["artist_id"])
            if artist_id not in used_artist_ids:
                if progress and count % 1_000_000 == 0:
                    print(
                        f"artists scanned for artifact metadata: {count:,}",
                        file=sys.stderr,
                        flush=True,
                    )
                continue

            node = artist_node(artist_id)
            item = {
                "node": node,
                "artist_id": artist_id,
                "gid": artist["gid"],
                "name": artist["name"],
                "sort_name": artist["sort_name"],
                "comment": artist["comment"],
                "degree": len(adjacency.get(node, [])),
            }
            artifacts.write_jsonl_item(handle, item)
            artist_meta[node] = item
            name_key = _add_index_value(by_name, artist["name"], node)
            _add_search_tokens(by_token, name_key, node)
            sort_key = _add_index_value(by_sort_name, artist["sort_name"], node)
            _add_search_tokens(by_token, sort_key, node)
            gid = artist["gid"]
            if isinstance(gid, str) and gid:
                by_mbid[gid.casefold()] = node
            found += 1
            if progress and count % 1_000_000 == 0:
                print(
                    f"artists scanned for artifact metadata: {count:,}",
                    file=sys.stderr,
                    flush=True,
                )

    for aliases_seen, alias in enumerate(mbdump.iter_artist_aliases(dump_dir), 1):
        node = artist_node(int(alias["artist_id"]))
        if node not in artist_meta:
            if progress and aliases_seen % 1_000_000 == 0:
                print(
                    f"artist aliases scanned for search index: {aliases_seen:,}",
                    file=sys.stderr,
                    flush=True,
                )
            continue
        alias_key = _add_index_value(by_alias, alias["name"], node)
        _add_search_tokens(by_token, alias_key, node)
        sort_key = _add_index_value(by_alias, alias["sort_name"], node)
        _add_search_tokens(by_token, sort_key, node)
        aliases_indexed += 1
        if progress and aliases_seen % 1_000_000 == 0:
            print(
                f"artist aliases scanned for search index: {aliases_seen:,}",
                file=sys.stderr,
                flush=True,
            )

    def sort_nodes(nodes: list[str] | set[str]) -> list[str]:
        return sorted(
            set(nodes),
            key=lambda node: (
                -int(artist_meta[node]["degree"]),
                str(artist_meta[node]["name"]).casefold(),
                node,
            )
        )

    for index in (by_name, by_sort_name, by_alias):
        for key, nodes in list(index.items()):
            index[key] = sort_nodes(nodes)

    artifacts.write_json(
        out_dir / artifacts.NAME_INDEX_FILE,
        {
            "by_mbid": by_mbid,
            "by_name": dict(by_name),
            "by_sort_name": dict(by_sort_name),
            "by_alias": dict(by_alias),
            "by_token": {
                token: sort_nodes(nodes) for token, nodes in sorted(by_token.items())
            },
        },
    )
    return {
        "artist_nodes": found,
        "used_artist_ids_missing_from_artist_table": len(used_artist_ids) - found,
        "artist_alias_rows_seen": aliases_seen,
        "artist_alias_rows_indexed": aliases_indexed,
    }


def build_graph(
    dump_dir: str | Path,
    out_dir: str | Path,
    max_artist_recordings: int = 100_000,
    progress: bool = True,
) -> dict[str, object]:
    # Build the full bipartite graph: artist nodes connected through connector nodes.
    dump_root = Path(dump_dir)
    mbdump.require_tables(dump_root, REQUIRED_TABLES)
    out_path = artifacts.prepare_out_dir(out_dir)

    if progress:
        print("Scanning artists for name-based ignores", file=sys.stderr, flush=True)
    ignored_artists = _ignored_by_name(dump_root, progress)
 
    if progress:
        print("Counting recordings per artist_credit", file=sys.stderr, flush=True)
    credit_recording_counts = _recording_counts_by_credit(dump_root, progress)

    if progress:
        print("Counting artist recording volume", file=sys.stderr, flush=True)
    over_limit, artist_volume_stats = _ignored_by_recording_volume(
        dump_root,
        credit_recording_counts,
        ignored_artists,
        max_artist_recordings,
        progress,
    )
    ignored_by_various = len(ignored_artists)
    ignored_artists.update(over_limit)

    if progress:
        print("Finding multi-artist recording credits", file=sys.stderr, flush=True)
    credit_artists, credit_stats = _multi_artist_credits(
        dump_root, credit_recording_counts, ignored_artists, progress
    )
    del credit_recording_counts

    adjacency: dict[str, list[str]] = {}
    used_artist_ids: set[int] = set()

    connectors_path = out_path / artifacts.CONNECTORS_FILE
    with connectors_path.open("w", encoding="utf-8") as connectors_handle:
        if progress:
            print("Writing recording connectors", file=sys.stderr, flush=True)
        recording_stats = _write_recording_connectors(
            dump_root,
            connectors_handle,
            adjacency,
            credit_artists,
            used_artist_ids,
            progress,
        )
        del credit_artists

        if progress:
            print("Writing membership connectors", file=sys.stderr, flush=True)
        membership_stats = _write_membership_connectors(
            dump_root,
            connectors_handle,
            adjacency,
            ignored_artists,
            used_artist_ids,
            progress,
        )

    if progress:
        print("Writing artist metadata and name index", file=sys.stderr, flush=True)
    artist_stats = _write_artists_and_index(
        dump_root, out_path, adjacency, used_artist_ids, progress
    )

    if progress:
        print("Writing adjacency pickle", file=sys.stderr, flush=True)
    artifacts.write_pickle(out_path / artifacts.ADJACENCY_FILE, adjacency)

    connector_count = int(recording_stats["recording_connectors"]) + int(
        membership_stats["membership_connectors"]
    )
    raw_edge_count = int(recording_stats["recording_raw_edges"]) + int(
        membership_stats["membership_raw_edges"]
    )
    meta: dict[str, object] = {
        "format": "music_mcn_graph_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "music_mcn_version": __version__,
        "dump_dir": str(dump_root),
        "max_artist_recordings": max_artist_recordings,
        "ignored_artists_by_various_name": ignored_by_various,
        "ignored_artists_over_recording_limit": len(over_limit),
        "total_ignored_artists": len(ignored_artists),
        "connector_nodes": connector_count,
        "raw_bipartite_edges": raw_edge_count,
        "adjacency_nodes": len(adjacency),
        **artist_volume_stats,
        **credit_stats,
        **recording_stats,
        **membership_stats,
        **artist_stats,
        "files": {
            "artists": artifacts.ARTISTS_FILE,
            "connectors": artifacts.CONNECTORS_FILE,
            "adjacency": artifacts.ADJACENCY_FILE,
            "name_index": artifacts.NAME_INDEX_FILE,
        },
    }
    artifacts.write_json(out_path / artifacts.META_FILE, meta)
    if progress:
        print(f"Done: wrote graph artifact to {out_path}", file=sys.stderr, flush=True)
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Music Collaboration Number graph")
    parser.add_argument("--dump-dir", required=True, help="Path to extracted MusicBrainz dump")
    parser.add_argument("--out", required=True, help="Output graph artifact directory")
    parser.add_argument(
        "--max-artist-recordings",
        type=int,
        default=100_000,
        help="Ignore artists credited on more than this many recordings",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress logging")
    args = parser.parse_args(argv)

    meta = build_graph(
        args.dump_dir,
        args.out,
        max_artist_recordings=args.max_artist_recordings,
        progress=not args.quiet,
    )
    print(f"Wrote {args.out}")
    print(
        f"Artists: {meta['artist_nodes']:,} | "
        f"connectors: {meta['connector_nodes']:,} | "
        f"raw edges: {meta['raw_bipartite_edges']:,}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
