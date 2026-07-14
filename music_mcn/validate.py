from __future__ import annotations

import argparse

from .search import ArtistLookupError, Graph, print_lookup_error, print_path


STRICT_CASES = [
    ("David Bowie", "Freddie Mercury", "eq", 2),
    ("Sophie Powers", "Lionel Richie", "lt", 6),
    ("Townes Van Zandt", "Underscores", "le", 4),
]

INFO_CASES = [
    ("Mike Patton", "Nico"),
]


def _passes(actual: int, operator: str, expected: int) -> bool:
    if operator == "eq":
        return actual == expected
    if operator == "lt":
        return actual < expected
    if operator == "le":
        return actual <= expected
    raise ValueError(f"unknown operator {operator!r}")


def _operator_text(operator: str) -> str:
    return {"eq": "==", "lt": "<", "le": "<="}[operator]


def run_validation(graph_dir: str) -> int:
    graph = Graph.load(graph_dir)
    failures = 0

    for source_name, target_name, operator, expected in STRICT_CASES:
        print(f"\n{source_name} -> {target_name}")
        try:
            source = graph.find_artist(source_name)
            target = graph.find_artist(target_name)
        except ArtistLookupError as exc:
            print_lookup_error(graph, exc)
            failures += 1
            continue

        path = graph.shortest_path(source, target)
        if path is None:
            print("FAIL: no path found")
            failures += 1
            continue

        hops = graph.display_hops(path)
        print_path(graph, source, target, path)
        if _passes(hops, operator, expected):
            print(f"PASS: {hops} {_operator_text(operator)} {expected}")
        else:
            print(f"FAIL: expected hops {_operator_text(operator)} {expected}, got {hops}")
            failures += 1

    for source_name, target_name in INFO_CASES:
        print(f"\n{source_name} -> {target_name}")
        try:
            source = graph.find_artist(source_name)
            target = graph.find_artist(target_name)
        except ArtistLookupError as exc:
            print_lookup_error(graph, exc)
            print("INFO: lookup failed for non-strict case")
            continue

        path = graph.shortest_path(source, target)
        print_path(graph, source, target, path)

    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Music Collaboration Number graph")
    parser.add_argument("--graph", required=True, help="Graph artifact directory")
    args = parser.parse_args(argv)
    return run_validation(args.graph)


if __name__ == "__main__":
    raise SystemExit(main())
