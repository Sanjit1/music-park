from __future__ import annotations

from pathlib import Path

from music_mcn import artifacts
from music_mcn.build_graph import build_graph


NULL = r"\N"
A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
UNKNOWN_ARTIST = "125ec42a-7229-4250-afc5-e057484327fe"
VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"
UNKNOWN_LABEL = "46caaa9e-3e26-49b5-827c-64ccc73c1b07"
NO_LABEL = "157afde4-4bf5-4039-8ad2-5a15acc85176"


def row(*values: object) -> str:
    return "\t".join(str(value) for value in values)


def artist_row(artist_id: int, gid: str, name: str) -> str:
    return row(artist_id, gid, name, name, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "")


def write_dump(root: Path, **tables: list[tuple[object, ...]]) -> None:
    root.mkdir()
    defaults = {
        "artist_alias": [],
        "label": [],
        "release_label": [],
        "link_type": [(103, NULL, 0, "member-link-type", "artist", "artist", "member of band")],
        "link": [],
        "l_artist_artist": [],
    }
    table_names = [
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
    for table in table_names:
        items = tables.get(table, defaults.get(table, []))
        lines = [artist_row(*item) if table == "artist" else row(*item) for item in items]
        content = "\n".join(lines)
        (root / table).write_text(f"{content}\n" if content else "", encoding="utf-8")


def connector_nodes(out_dir: Path) -> set[str]:
    return {item["node"] for item in artifacts.iter_jsonl(out_dir / artifacts.CONNECTORS_FILE)}


def artist_mbids(out_dir: Path) -> set[str]:
    return {item["gid"] for item in artifacts.iter_jsonl(out_dir / artifacts.ARTISTS_FILE)}


def test_filtered_artist_mbid_rejects_credit_even_when_credited_as_alias(tmp_path):
    dump_dir = tmp_path / "dump"
    out_dir = tmp_path / "graph"
    write_dump(
        dump_dir,
        artist=[
            (1, A, "Alpha"),
            (2, B, "Beta"),
            (3, UNKNOWN_ARTIST, "Unknown Artist Alias"),
        ],
        artist_credit_name=[
            (10, 0, 1, "Alpha"),
            (12, 0, 1, "Alpha"),
            (12, 1, 2, "Beta"),
            (13, 0, 1, "Alpha"),
            (13, 1, 3, "[christmas music]"),
        ],
        recording=[
            (100, "rec-valid", "Good Song", 12),
            (101, "rec-special", "Alias Credit", 13),
            (102, "rec-title", "[untitled]", 12),
        ],
        release_group=[(1000, "rg-valid", "Valid Release Group", 10)],
        release=[(2000, "rel-valid", "Valid Release", 10, 1000)],
        medium=[(3000, 2000)],
        track=[
            (4000, "track-valid", 100, 3000, 1, "1", "Good Song", 12),
            (4001, "track-special", 101, 3000, 2, "2", "Alias Credit", 13),
            (4002, "track-title", 102, 3000, 3, "3", "Good Track Name", 12),
        ],
    )

    meta = build_graph(dump_dir, out_dir, progress=False)

    assert connector_nodes(out_dir) == {"r:100"}
    assert UNKNOWN_ARTIST not in artist_mbids(out_dir)
    assert meta["artist_credits_with_filtered_artists"] == 1
    assert meta["recordings_filtered_by_recording_credit"] == 1
    assert meta["recordings_filtered_by_title"] == 1


def test_recording_needs_at_least_one_acceptable_non_va_release_context(tmp_path):
    dump_dir = tmp_path / "dump"
    out_dir = tmp_path / "graph"
    write_dump(
        dump_dir,
        artist=[(1, A, "Alpha"), (2, B, "Beta"), (4, VARIOUS_ARTISTS, "Compilation Credit")],
        artist_credit_name=[
            (10, 0, 1, "Alpha"),
            (12, 0, 1, "Alpha"),
            (12, 1, 2, "Beta"),
            (20, 0, 4, "Various Artists"),
        ],
        recording=[
            (100, "rec-mixed-contexts", "Shared Song", 12),
            (101, "rec-va-only", "Compilation Only", 12),
        ],
        release_group=[
            (1000, "rg-valid", "Valid Release Group", 10),
            (1001, "rg-va", "Compilation Release Group", 20),
        ],
        release=[
            (2000, "rel-valid", "Valid Release", 10, 1000),
            (2001, "rel-va", "Compilation Release", 20, 1001),
        ],
        medium=[(3000, 2000), (3001, 2001)],
        track=[
            (4000, "track-va-context", 100, 3001, 1, "1", "Shared Song", 12),
            (4001, "track-valid-context", 100, 3000, 1, "1", "Shared Song", 12),
            (4002, "track-va-only", 101, 3001, 2, "2", "Compilation Only", 12),
        ],
    )

    meta = build_graph(dump_dir, out_dir, progress=False)

    assert connector_nodes(out_dir) == {"r:100"}
    assert VARIOUS_ARTISTS not in artist_mbids(out_dir)
    assert meta["tracks_filtered_by_release_credit"] == 2
    assert meta["recordings_without_accepted_release_context"] == 1


def test_unknown_label_rejects_release_context_but_no_label_is_allowed(tmp_path):
    dump_dir = tmp_path / "dump"
    out_dir = tmp_path / "graph"
    write_dump(
        dump_dir,
        artist=[(1, A, "Alpha"), (2, B, "Beta")],
        artist_credit_name=[(10, 0, 1, "Alpha"), (12, 0, 1, "Alpha"), (12, 1, 2, "Beta")],
        recording=[
            (100, "rec-unknown-label", "Unknown Label Song", 12),
            (101, "rec-no-label", "Self Release Song", 12),
        ],
        release_group=[
            (1000, "rg-unknown-label", "Unknown Label Group", 10),
            (1001, "rg-no-label", "No Label Group", 10),
        ],
        release=[
            (2000, "rel-unknown-label", "Unknown Label Release", 10, 1000),
            (2001, "rel-no-label", "No Label Release", 10, 1001),
        ],
        medium=[(3000, 2000), (3001, 2001)],
        track=[
            (4000, "track-unknown-label", 100, 3000, 1, "1", "Unknown Label Song", 12),
            (4001, "track-no-label", 101, 3001, 1, "1", "Self Release Song", 12),
        ],
        label=[(90, UNKNOWN_LABEL, "[unknown]"), (91, NO_LABEL, "[no label]")],
        release_label=[(5000, 2000, 90), (5001, 2001, 91)],
    )

    meta = build_graph(dump_dir, out_dir, progress=False)

    assert connector_nodes(out_dir) == {"r:101"}
    assert meta["tracks_filtered_by_release_label"] == 1
    assert meta["releases_with_filtered_labels"] == 1
