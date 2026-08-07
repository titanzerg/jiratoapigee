#!/usr/bin/env python3
"""Summarize latest Jira card per base path for each environment."""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime
from typing import Any


DEFAULT_INPUT = "data/jira_export/jira_api_support_export.csv"
DEFAULT_OUTPUT_PREFIX = "data/summarize_latest_by_env/jira_latest_by_basepath"
ENVS = ("dev", "qa", "uat", "prod")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read jira_api_support_export.csv and create latest-card summaries by environment."
    )
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output-prefix", default=DEFAULT_OUTPUT_PREFIX)
    return parser.parse_args()


def is_true(value: str) -> bool:
    return value.strip().lower() in {"true", "t", "yes", "y", "1"}


def parse_created_date(value: str) -> datetime:
    cleaned = value.strip()
    if not cleaned:
        return datetime.min

    candidates = [
        cleaned,
        cleaned.replace("Z", "+0000"),
    ]

    for candidate in candidates:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                pass

    return datetime.min


def split_basepaths(value: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for item in value.split(","):
        path = item.strip()
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def latest_by_env(input_path: str) -> dict[str, dict[str, dict[str, Any]]]:
    latest: dict[str, dict[str, dict[str, Any]]] = {env: {} for env in ENVS}

    with open(input_path, newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        for row in reader:
            basepaths = split_basepaths(row.get("basepath", ""))
            if not basepaths:
                continue

            created = parse_created_date(row.get("create date", ""))
            for env in ENVS:
                if not is_true(row.get(env, "")):
                    continue

                for basepath in basepaths:
                    current = latest[env].get(basepath)
                    if current is None or created > current["created"]:
                        latest[env][basepath] = {
                            "base path": basepath,
                            "card": row.get("link", ""),
                            "created": created,
                        }

    return latest


def write_outputs(summary: dict[str, dict[str, dict[str, Any]]], output_prefix: str) -> None:
    for env in ENVS:
        output_path = f"{output_prefix}_{env}.csv"
        rows = sorted(summary[env].values(), key=lambda row: row["base path"])
        ensure_parent_dir(output_path)
        with open(output_path, "w", newline="", encoding="utf-8-sig") as output:
            writer = csv.DictWriter(output, fieldnames=["base path", "card"])
            writer.writeheader()
            for row in rows:
                writer.writerow({"base path": row["base path"], "card": row["card"]})
        print(f"Wrote {len(rows)} row(s) to {output_path}")


def ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def main() -> int:
    args = parse_args()
    summary = latest_by_env(args.input)
    write_outputs(summary, args.output_prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
