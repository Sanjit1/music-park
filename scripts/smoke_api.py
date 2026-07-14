#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def get_json(
    base_url: str,
    path: str,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)
    url = base_url.rstrip("/") + path + query
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {"error": body}
        return exc.code, data


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        print(f"PASS {name}{': ' + detail if detail else ''}")
        return True
    print(f"FAIL {name}{': ' + detail if detail else ''}")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test the MCN API over HTTP")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--admin-token", default=None)
    args = parser.parse_args(argv)

    failures = 0
    try:
        status, health = get_json(args.base_url, "/health")
        failures += not check("/health", status == 200 and health.get("status") == "ok")

        status, artists = get_json(args.base_url, "/artists/search", {"q": "Bruce Springsteen"})
        found_springsteen = any(
            candidate.get("name") == "Bruce Springsteen"
            for candidate in artists.get("candidates", [])
        )
        failures += not check(
            "/artists/search", status == 200 and found_springsteen, "Bruce Springsteen found"
        )

        path_params = {"source": "Bruce Springsteen", "target": "Freddie Mercury"}
        status, first = get_json(args.base_url, "/mcn/path", path_params)
        failures += not check(
            "/mcn/path first",
            status == 200 and first.get("hop_count") == 2,
            f"cache={first.get('cache')} hops={first.get('hop_count')}",
        )

        status, second = get_json(args.base_url, "/mcn/path", path_params)
        failures += not check(
            "/mcn/path repeat",
            status == 200 and second.get("hop_count") == 2 and second.get("cache") == "hit",
            f"cache={second.get('cache')} hops={second.get('hop_count')}",
        )

        headers = {"X-MCN-Admin-Token": args.admin_token} if args.admin_token else None
        status, stats = get_json(args.base_url, "/cache/stats", headers=headers)
        if status == 404:
            failures += not check("/cache/stats", True, "disabled")
        else:
            failures += not check(
                "/cache/stats",
                status == 200 and int(stats.get("cached_full_paths", 0)) >= 1,
                f"status={status} cached_full_paths={stats.get('cached_full_paths')}",
            )
    except Exception as exc:
        print(f"FAIL smoke_api: {exc}", file=sys.stderr)
        return 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
