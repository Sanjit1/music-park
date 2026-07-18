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
    "release_group",
    "release",
    "medium",
    "track",
    "label",
    "release_label",
    "link_type",
    "link",
    "l_artist_artist",
]

FILTER_ARTIST_MBIDS = frozenset(
    {
        # Official MusicBrainz special-purpose artists.
        "f731ccc4-e22a-43af-a747-64213329e088",  # [anonymous]
        "33cf029c-63b0-41a0-9855-be2a3665fb3b",  # [data]
        "314e1c25-dde7-4e4d-b2f4-0a7b9f7c56dc",  # [dialogue]
        "eec63d3c-3b81-4ad4-b1e4-7c147d4d2b61",  # [no artist]
        "9be7f096-97ec-4615-8957-8d40b5dcbc41",  # [traditional]
        "125ec42a-7229-4250-afc5-e057484327fe",  # [unknown]
        "89ad4ac3-39f7-470e-963a-56509c546377",  # Various Artists
        "7e84f845-ac16-41fe-9ff8-df12eb32af55",  # MusicBrainz Test Artist
        # Separately represented special-purpose subsets.
        "66ea0139-149f-4a0c-8fbf-5ea9ec4a6e49",  # [Disney]
        "a0ef7e1d-44ff-4039-9435-7d5fefdeecc9",  # [theatre]
        "90068d37-bae7-4292-be4a-704c145bd616",  # [church chimes]
        "80a8851f-444c-4539-892b-ad2a49292aa9",  # [language instruction]
    }
)

FILTER_LABEL_MBIDS = frozenset(
    {
        "46caaa9e-3e26-49b5-827c-64ccc73c1b07",  # [unknown]
        "02442aba-cf00-445c-877e-f0eaa504d8c2",  # MusicBrainz Test Label
    }
)

FILTER_TRACK_TITLES = frozenset(
    {
        "[unknown]",
        "[untitled]",
        "[data track]",
        "[silence]",
        "[crowd noise]",
        "[guitar solo]",
    }
)


artist_node = lambda artist_id: f"{ARTIST_PREFIX}{artist_id}"
recording_node = lambda recording_id: f"{RECORDING_PREFIX}{recording_id}"
membership_node = lambda relationship_id: f"{MEMBERSHIP_PREFIX}{relationship_id}"


def _artist_type_label(artist_type: int | None) -> str | None:
    if artist_type == 1:
        return "Person"
    return "Group"

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


def _is_filtered_title(title: object) -> bool:
    return str(title or "").strip().casefold() in FILTER_TRACK_TITLES


def _log_progress(progress: bool, count: int, label: str) -> None:
    if progress and count % 1_000_000 == 0:
        print(f"{label}: {count:,}", file=sys.stderr, flush=True)


def _ids_for_mbids(rows, id_key: str, mbids: frozenset[str]) -> set[int]:
    matched: set[int] = set()
    for row in rows:
        gid = row["gid"]
        if isinstance(gid, str) and gid.casefold() in mbids:
            matched.add(int(row[id_key]))
    return matched


def _credit_ids_with_filtered_artists(
    dump_dir: Path,
    filtered_artist_ids: set[int],
    progress: bool,
) -> set[int]:
    filtered_credit_ids: set[int] = set()
    for count, (credit_id, _position, artist_id, _name) in enumerate(
        mbdump.iter_artist_credit_names(dump_dir), 1
    ):
        if artist_id in filtered_artist_ids:
            filtered_credit_ids.add(credit_id)
        _log_progress(progress, count, "artist credit rows scanned for filtered MBIDs")
    return filtered_credit_ids


def _recordings_with_accepted_contexts(
    dump_dir: Path,
    filtered_credit_ids: set[int],
    progress: bool,
) -> tuple[set[int], dict[str, int]]:
    filtered_label_ids = {
        label_id
        for label_id, gid in mbdump.iter_labels(dump_dir)
        if isinstance(gid, str) and gid.casefold() in FILTER_LABEL_MBIDS
    }
    releases_with_filtered_labels = {
        release_id
        for release_id, label_id in mbdump.iter_release_labels(dump_dir)
        if label_id in filtered_label_ids
    }
    release_group_credits = dict(mbdump.iter_release_groups(dump_dir))
    release_contexts = {}
    for count, (release_id, release_credit_id, group_id) in enumerate(
        mbdump.iter_releases(dump_dir), 1
    ):
        group_credit_id = release_group_credits.get(group_id)
        if group_credit_id is not None:
            release_contexts[release_id] = (
                release_credit_id,
                group_credit_id,
                release_id in releases_with_filtered_labels,
            )
        _log_progress(progress, count, "releases loaded for contexts")

    medium_contexts = {}
    for count, (medium_id, release_id) in enumerate(mbdump.iter_media(dump_dir), 1):
        context = release_contexts.get(release_id)
        if context is not None:
            medium_contexts[medium_id] = context
        _log_progress(progress, count, "media loaded for release contexts")

    accepted_recording_ids: set[int] = set()
    stats = defaultdict(int)
    tracks_seen = 0
    for tracks_seen, (recording_id, medium_id, title, track_credit_id) in enumerate(
        mbdump.iter_tracks(dump_dir), 1
    ):
        if _is_filtered_title(title):
            stats["tracks_filtered_by_title"] += 1
        else:
            release_context = medium_contexts.get(medium_id)
            if release_context is None:
                stats["tracks_missing_release_context"] += 1
            elif track_credit_id in filtered_credit_ids:
                stats["tracks_filtered_by_track_credit"] += 1
            else:
                release_credit_id, release_group_credit_id, release_has_filtered_label = (
                    release_context
                )
                if release_has_filtered_label:
                    stats["tracks_filtered_by_release_label"] += 1
                elif release_credit_id in filtered_credit_ids:
                    stats["tracks_filtered_by_release_credit"] += 1
                elif release_group_credit_id in filtered_credit_ids:
                    stats["tracks_filtered_by_release_group_credit"] += 1
                else:
                    accepted_recording_ids.add(recording_id)
                    stats["accepted_track_contexts"] += 1
        _log_progress(progress, tracks_seen, "tracks scanned for release-aware contexts")

    stats.update(
        {
            "tracks_seen": tracks_seen,
            "filtered_label_ids": len(filtered_label_ids),
            "releases_with_filtered_labels": len(releases_with_filtered_labels),
            "recordings_with_accepted_release_context": len(accepted_recording_ids),
        }
    )
    stats.setdefault("tracks_filtered_by_title", 0)
    stats.setdefault("tracks_filtered_by_track_credit", 0)
    stats.setdefault("tracks_filtered_by_release_credit", 0)
    stats.setdefault("tracks_filtered_by_release_group_credit", 0)
    stats.setdefault("tracks_filtered_by_release_label", 0)
    stats.setdefault("tracks_missing_release_context", 0)
    return accepted_recording_ids, dict(stats)


def _recording_counts_by_credit(dump_dir: Path, progress: bool) -> dict[int, int]:
    # First pass: learn how often each artist_credit appears on recordings.
    counts: dict[int, int] = defaultdict(int)
    for count, recording in enumerate(mbdump.iter_recordings(dump_dir), 1):
        counts[int(recording["artist_credit_id"])] += 1
        if progress and count % 1_000_000 == 0:
            print(f"recordings counted: {count:,}", file=sys.stderr, flush=True)
    return dict(counts)


def _multi_artist_credits(
    dump_dir: Path,
    credit_recording_counts: dict[int, int],
    filtered_artist_ids: set[int],
    filtered_credit_ids: set[int],
    progress: bool,
) -> tuple[dict[int, list[tuple[int, str]]], dict[str, int]]:
    # Only multi-artist credits can create recording-based artist links.
    kept_counts: dict[int, int] = defaultdict(int)
    for count, (credit_id, _position, artist_id, _name) in enumerate(
        mbdump.iter_artist_credit_names(dump_dir), 1
    ):
        if (
            credit_id not in filtered_credit_ids
            and artist_id not in filtered_artist_ids
            and credit_id in credit_recording_counts
        ):
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
        if (
            credit_id in multi_credit_ids
            and credit_id not in filtered_credit_ids
            and artist_id not in filtered_artist_ids
        ):
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


def _write_recording_connectors(
    dump_dir: Path,
    connectors_handle: TextIO,
    adjacency: dict[str, list[str]],
    credit_artists: dict[int, list[tuple[int, str]]],
    filtered_credit_ids: set[int],
    accepted_release_context_recording_ids: set[int],
    used_artist_ids: set[int],
    progress: bool,
) -> dict[str, int]:
    # Recording connectors form artist -> recording -> artist paths.
    recording_connectors = 0
    recording_edges = 0
    recordings_seen = 0
    recordings_filtered_by_title = 0
    recordings_filtered_by_recording_credit = 0
    recordings_without_accepted_release_context = 0
    for recordings_seen, recording in enumerate(mbdump.iter_recordings(dump_dir), 1):
        recording_id = int(recording["recording_id"])
        artist_credit_id = int(recording["artist_credit_id"])
        if _is_filtered_title(recording["title"]):
            recordings_filtered_by_title += 1
        elif artist_credit_id in filtered_credit_ids:
            recordings_filtered_by_recording_credit += 1
        elif recording_id not in accepted_release_context_recording_ids:
            recordings_without_accepted_release_context += 1
        else:
            artists_for_credit = credit_artists.get(artist_credit_id)
            if artists_for_credit:
                node = recording_node(recording_id)
                artifacts.write_jsonl_item(
                    connectors_handle,
                    {
                        "node": node,
                        "type": "recording",
                        "recording_id": recording["recording_id"],
                        "gid": recording["gid"],
                        "title": recording["title"],
                        "artist_credit_id": artist_credit_id,
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
        _log_progress(progress, recordings_seen, "recordings scanned for connectors")

    return {
        "recordings_seen": recordings_seen,
        "recording_connectors": recording_connectors,
        "recording_raw_edges": recording_edges,
        "recordings_filtered_by_title": recordings_filtered_by_title,
        "recordings_filtered_by_recording_credit": recordings_filtered_by_recording_credit,
        "recordings_without_accepted_release_context": (
            recordings_without_accepted_release_context
        ),
    }


def _write_membership_connectors(
    dump_dir: Path,
    connectors_handle: TextIO,
    adjacency: dict[str, list[str]],
    filtered_artist_ids: set[int],
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
        if artist0 in filtered_artist_ids or artist1 in filtered_artist_ids or artist0 == artist1:
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
                "artist_type": _artist_type_label(artist.get("artist_type")),
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
    progress: bool = True,
) -> dict[str, object]:
    # Build the full bipartite graph: artist nodes connected through connector nodes.
    dump_root = Path(dump_dir)
    mbdump.require_tables(dump_root, REQUIRED_TABLES)
    out_path = artifacts.prepare_out_dir(out_dir)

    if progress:
        print("Scanning artists for filtered MBIDs", file=sys.stderr, flush=True)
    filtered_artist_ids = _ids_for_mbids(
        mbdump.iter_artists(dump_root), "artist_id", FILTER_ARTIST_MBIDS
    )

    if progress:
        print("Scanning artist credits for filtered artists", file=sys.stderr, flush=True)
    filtered_credit_ids = _credit_ids_with_filtered_artists(
        dump_root,
        filtered_artist_ids,
        progress,
    )

    if progress:
        print("Scanning release-aware contexts", file=sys.stderr, flush=True)
    accepted_release_context_recording_ids, release_context_stats = (
        _recordings_with_accepted_contexts(
            dump_root,
            filtered_credit_ids,
            progress,
        )
    )

    if progress:
        print("Counting recordings per artist_credit", file=sys.stderr, flush=True)
    credit_recording_counts = _recording_counts_by_credit(dump_root, progress)

    if progress:
        print("Finding multi-artist recording credits", file=sys.stderr, flush=True)
    credit_artists, credit_stats = _multi_artist_credits(
        dump_root,
        credit_recording_counts,
        filtered_artist_ids,
        filtered_credit_ids,
        progress,
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
            filtered_credit_ids,
            accepted_release_context_recording_ids,
            used_artist_ids,
            progress,
        )
        del credit_artists
        del accepted_release_context_recording_ids

        if progress:
            print("Writing membership connectors", file=sys.stderr, flush=True)
        membership_stats = _write_membership_connectors(
            dump_root,
            connectors_handle,
            adjacency,
            filtered_artist_ids,
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
        "filtered_titles": sorted(FILTER_TRACK_TITLES),
        "filtered_artist_mbids": sorted(FILTER_ARTIST_MBIDS),
        "filtered_label_mbids": sorted(FILTER_LABEL_MBIDS),
        "filtered_artist_ids": len(filtered_artist_ids),
        "artist_credits_with_filtered_artists": len(filtered_credit_ids),
        "connector_nodes": connector_count,
        "raw_bipartite_edges": raw_edge_count,
        "adjacency_nodes": len(adjacency),
        **release_context_stats,
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
    parser.add_argument("--quiet", action="store_true", help="Suppress progress logging")
    args = parser.parse_args(argv)

    meta = build_graph(
        args.dump_dir,
        args.out,
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
