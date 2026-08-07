#!/usr/bin/env python3
"""Guess better Apigee basepaths for low-confidence Jira export rows."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime

from add_apigee_proxy_name import (
    DEFAULT_BUNDLE_CACHE_DIR,
    DEFAULT_ORG,
    DEFAULT_SWAGGER_CACHE_DIR,
    build_proxy_info_from_db,
    get_access_token,
    get_jira_swagger_paths,
    normalize_basepath,
)
from guess_missing_proxy import (
    build_basepath_flow_index,
    build_flow_index,
    build_proxy_basepaths,
    flatten_proxy_infos,
    guess_for_card,
)


DEFAULT_INPUT = "data/jira_export/jira_api_support_export.csv"
DEFAULT_OUTPUT = "data/guess_low_confidence_basepath/jira_low_confidence_basepath_guess.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guess better basepaths for low-confidence jira_export rows.")
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--confident", type=int, default=70, help="Select rows with confidence below this percent.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--org", default=os.getenv("APIGEE_ORG", DEFAULT_ORG))
    parser.add_argument("--token", help="OAuth access token. Defaults to `gcloud auth print-access-token`.")
    parser.add_argument("--bundle-cache-dir", default=DEFAULT_BUNDLE_CACHE_DIR)
    parser.add_argument("--swagger-cache-dir", default=DEFAULT_SWAGGER_CACHE_DIR)
    parser.add_argument("--limit", type=int, default=0, help="Maximum selected rows to process. Use 0 for no limit.")
    return parser.parse_args()


def parse_created_date(value: str) -> datetime:
    cleaned = value.strip()
    if not cleaned:
        return datetime.min
    for candidate in (cleaned, cleaned.replace("Z", "+0000")):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=None)
            except ValueError:
                pass
    return datetime.min


def confidence_values(value: str) -> list[int]:
    return [int(match) for match in re.findall(r"(\d+)%", value or "")]


def is_low_confidence(row: dict[str, str], threshold: int) -> bool:
    values = confidence_values(row.get("confident", ""))
    if not values:
        return True
    return min(values) < threshold


def split_basepaths(value: str) -> set[str]:
    result: set[str] = set()
    for item in (value or "").split(","):
        normalized = normalize_basepath(item.strip())
        if normalized:
            result.add(normalized)
    return result


def read_selected_rows(path: str, threshold: int) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with open(path, newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        for row in reader:
            if is_low_confidence(row, threshold):
                rows.append(row)
    return rows


def ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def sort_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    def guess_confidence(row: dict[str, str]) -> int:
        values = confidence_values(row.get("guess_confident", ""))
        return values[0] if values else -1

    return sorted(
        rows,
        key=lambda row: (
            guess_confidence(row),
            parse_created_date(row.get("create date", "")),
            row.get("link", ""),
        ),
        reverse=True,
    )


def write_output(path: str, rows: list[dict[str, str]], input_fieldnames: list[str]) -> None:
    extra_fields = ["guess_basepath", "guess_proxy", "guess_confident"]
    fieldnames = input_fieldnames + [field for field in extra_fields if field not in input_fieldnames]
    ensure_parent_dir(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def main() -> int:
    args = parse_args()
    selected_rows = read_selected_rows(args.input, args.confident)
    if args.limit:
        selected_rows = selected_rows[: args.limit]
    print(f"Selected {len(selected_rows)} low-confidence row(s) from {args.input}")

    with open(args.input, newline="", encoding="utf-8-sig") as source:
        input_fieldnames = list(csv.DictReader(source).fieldnames or [])

    proxy_info_by_basepath = build_proxy_info_from_db(args.env_file)
    proxy_infos = flatten_proxy_infos(proxy_info_by_basepath)
    proxy_basepaths = build_proxy_basepaths(proxy_info_by_basepath)
    try:
        token = get_access_token(args.token)
    except RuntimeError as error:
        print(f"warning: {error}", file=sys.stderr)
        print("warning: continuing with existing Apigee bundle cache only", file=sys.stderr)
        token = ""

    flow_index = build_flow_index(proxy_infos, args.org, token, args.bundle_cache_dir)
    basepath_flow_index = build_basepath_flow_index(proxy_infos, args.org, token, args.bundle_cache_dir)
    print(f"Loaded comparable flow paths for {len(flow_index)} proxy revision(s)")

    output_rows: list[dict[str, str]] = []
    swagger_cache: dict[str, list[str]] = {}
    total = len(selected_rows)
    for index, row in enumerate(selected_rows, start=1):
        if index % 25 == 0:
            print(f"guessed {index}/{total} row(s)", file=sys.stderr)

        card = row.get("link", "").strip()
        basepaths = split_basepaths(row.get("basepath", ""))
        guess = guess_for_card(
            card,
            basepaths,
            flow_index,
            basepath_flow_index,
            proxy_basepaths,
            args.env_file,
            args.swagger_cache_dir,
            swagger_cache,
        )

        output_row = dict(row)
        output_row["guess_basepath"] = guess.desc_basepath.removeprefix("basepath: ").strip()
        output_row["guess_proxy"] = guess.proxy
        output_row["guess_confident"] = f"{guess.confidence}%" if guess.confidence > 0 else ""
        output_rows.append(output_row)

    output_rows = sort_rows(output_rows)
    write_output(args.output, output_rows, input_fieldnames)
    print(f"Wrote {len(output_rows)} row(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
