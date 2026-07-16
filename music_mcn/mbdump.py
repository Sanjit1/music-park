from __future__ import annotations

from pathlib import Path
from typing import Iterator, Sequence


NULL = r"\N"


class DumpError(RuntimeError):
    pass


def clean(value: str) -> str | None:
    return None if value == NULL else value


def as_int(value: str, table: str, line_no: int, column: int) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise DumpError(
            f"{table}:{line_no}: expected integer in column {column}, got {value!r}"
        ) from exc


def table_path(dump_dir: str | Path, table: str) -> Path:
    root = Path(dump_dir)
    direct = root / table
    if direct.exists():
        return direct

    nested = root / "mbdump" / table
    if nested.exists():
        return nested

    raise DumpError(
        f"Could not find MusicBrainz table {table!r} under {root} "
        f"(tried {direct} and {nested})"
    )


def iter_rows(
    dump_dir: str | Path, table: str, min_columns: int
) -> Iterator[tuple[int, list[str]]]:
    path = table_path(dump_dir, table)
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.rstrip("\n")
            if line.endswith("\r"):
                line = line[:-1]
            row = line.split("\t")
            if len(row) < min_columns:
                raise DumpError(
                    f"{table}:{line_no}: expected at least {min_columns} columns, "
                    f"got {len(row)}"
                )
            yield line_no, row


def iter_artists(dump_dir: str | Path) -> Iterator[dict[str, int | str | None]]:
    # artist: id, gid, name, sort_name, ..., comment, ...
    for line_no, row in iter_rows(dump_dir, "artist", 14):
        yield {
            "artist_id": as_int(row[0], "artist", line_no, 0),
            "gid": clean(row[1]),
            "name": clean(row[2]) or "",
            "sort_name": clean(row[3]),
            "comment": clean(row[13]),
        }


def iter_artist_aliases(dump_dir: str | Path) -> Iterator[dict[str, int | str | None]]:
    # artist_alias: id, artist, name, locale, ..., type, sort_name, ...
    for line_no, row in iter_rows(dump_dir, "artist_alias", 8):
        yield {
            "artist_id": as_int(row[1], "artist_alias", line_no, 1),
            "name": clean(row[2]) or "",
            "sort_name": clean(row[7]),
        }


def iter_recordings(dump_dir: str | Path) -> Iterator[dict[str, int | str | None]]:
    # recording: id, gid, name, artist_credit, ...
    for line_no, row in iter_rows(dump_dir, "recording", 4):
        yield {
            "recording_id": as_int(row[0], "recording", line_no, 0),
            "gid": clean(row[1]),
            "title": clean(row[2]) or "",
            "artist_credit_id": as_int(row[3], "recording", line_no, 3),
        }


def iter_artist_credit_names(
    dump_dir: str | Path,
) -> Iterator[tuple[int, int, int, str]]:
    # artist_credit_name: artist_credit, position, artist, name, join_phrase
    for line_no, row in iter_rows(dump_dir, "artist_credit_name", 4):
        yield (
            as_int(row[0], "artist_credit_name", line_no, 0),
            as_int(row[1], "artist_credit_name", line_no, 1),
            as_int(row[2], "artist_credit_name", line_no, 2),
            clean(row[3]) or "",
        )


def find_member_of_band_link_types(dump_dir: str | Path) -> dict[int, str]:
    link_types: dict[int, str] = {}
    # link_type: id, parent, child_order, gid, entity_type0, entity_type1, name, ...
    for line_no, row in iter_rows(dump_dir, "link_type", 7):
        entity0 = clean(row[4])
        entity1 = clean(row[5])
        name = clean(row[6]) or ""
        lowered = name.casefold()
        if entity0 == "artist" and entity1 == "artist" and "member of band" in lowered:
            link_types[as_int(row[0], "link_type", line_no, 0)] = name
    return link_types


def iter_links(dump_dir: str | Path) -> Iterator[tuple[int, int]]:
    # link: id, link_type, date fields, ...
    for line_no, row in iter_rows(dump_dir, "link", 2):
        yield (
            as_int(row[0], "link", line_no, 0),
            as_int(row[1], "link", line_no, 1),
        )


def iter_artist_artist_links(dump_dir: str | Path) -> Iterator[tuple[int, int, int, int]]:
    # l_artist_artist: id, link, entity0, entity1, ...
    for line_no, row in iter_rows(dump_dir, "l_artist_artist", 4):
        yield (
            as_int(row[0], "l_artist_artist", line_no, 0),
            as_int(row[1], "l_artist_artist", line_no, 1),
            as_int(row[2], "l_artist_artist", line_no, 2),
            as_int(row[3], "l_artist_artist", line_no, 3),
        )


def require_tables(dump_dir: str | Path, tables: Sequence[str]) -> None:
    for table in tables:
        table_path(dump_dir, table)
# fire
