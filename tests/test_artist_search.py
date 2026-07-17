from __future__ import annotations

from pathlib import Path

from music_mcn.search import Graph, normalize_name


def test_normalize_name_handles_punctuation_and_accents():
    assert normalize_name("M. Shadows") == "m shadows"
    assert normalize_name("Beyonce\u0301") == "beyonce"
    assert normalize_name("AC/DC") == "ac dc"


def test_artist_search_ranks_exact_alias_and_fallback_matches():
    graph = Graph(
        Path("tiny"),
        adjacency={
            "a:1": ["r:1"],
            "a:2": ["r:2"],
            "a:3": ["r:3"],
        },
        artists={
            "a:1": {
                "node": "a:1",
                "artist_id": 1,
                "gid": "m-shadows",
                "name": "M. Shadows",
                "sort_name": "Shadows, M.",
                "degree": 100,
            },
            "a:2": {
                "node": "a:2",
                "artist_id": 2,
                "gid": "shadow-m",
                "name": "Shadow M",
                "sort_name": "Shadow M",
                "degree": 10,
            },
            "a:3": {
                "node": "a:3",
                "artist_id": 3,
                "gid": "avenged-sevenfold",
                "name": "Avenged Sevenfold",
                "sort_name": "Avenged Sevenfold",
                "degree": 200,
            },
        },
        name_index={
            "by_mbid": {"avenged-sevenfold": "a:3"},
            "by_name": {
                "m shadows": ["a:1"],
                "shadow m": ["a:2"],
                "avenged sevenfold": ["a:3"],
            },
            "by_sort_name": {
                "shadows m": ["a:1"],
                "shadow m": ["a:2"],
                "avenged sevenfold": ["a:3"],
            },
            "by_alias": {
                "a7x": ["a:3"],
            },
            "by_token": {
                "m": ["a:1", "a:2"],
                "shadows": ["a:1"],
                "shadow": ["a:2"],
                "avenged": ["a:3"],
                "sevenfold": ["a:3"],
                "a7x": ["a:3"],
            },
        },
        meta={},
    )

    m_shadows = graph.search_artist_candidates("m shadows", limit=5)
    assert m_shadows[0]["node"] == "a:1"
    assert m_shadows[0]["match_reason"] == "name_exact"

    partial = graph.search_artist_candidates("shad m", limit=5)
    assert partial[0]["node"] == "a:1"
    assert partial[0]["match_reason"] == "token_prefix"

    a7x = graph.search_artist_candidates("a7x", limit=5)
    assert a7x[0]["node"] == "a:3"
    assert a7x[0]["match_reason"] == "alias_exact"
    assert graph.find_artist("a7x") == "a:3"

    short_alias_piece = graph.search_artist_candidates("7x", limit=5)
    assert short_alias_piece[0]["node"] == "a:3"
    assert short_alias_piece[0]["match_reason"] == "alias_contains_fallback"


def test_artist_search_uses_fallback_alias_for_older_indexes():
    graph = Graph(
        Path("tiny"),
        adjacency={"a:3": ["r:3"]},
        artists={
            "a:3": {
                "node": "a:3",
                "artist_id": 3,
                "gid": "avenged-sevenfold",
                "name": "Avenged Sevenfold",
                "sort_name": "Avenged Sevenfold",
                "degree": 200,
            },
        },
        name_index={
            "by_mbid": {},
            "by_name": {"avenged sevenfold": ["a:3"]},
            "by_sort_name": {"avenged sevenfold": ["a:3"]},
            "by_alias": {},
            "by_token": {
                "avenged": ["a:3"],
                "sevenfold": ["a:3"],
            },
        },
        meta={},
    )

    a7x = graph.search_artist_candidates("a7x", limit=5)
    assert a7x[0]["node"] == "a:3"
    assert a7x[0]["match_reason"] == "alias_exact"
    assert graph.find_artist("a7x") == "a:3"
