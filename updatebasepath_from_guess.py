#!/usr/bin/env python3
"""Batch update Jira basepath descriptions from guess_missing_proxy output."""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

from jira_export import JiraClient, JiraConfig


def load_updatebasepath_module():
    path = os.path.join(os.path.dirname(__file__), "updatebasepath")
    loader = SourceFileLoader("updatebasepath_script", path)
    spec = spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("Cannot load updatebasepath helper")
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


UPDATEBASEPATH = load_updatebasepath_module()
DEFAULT_BASE_URL = UPDATEBASEPATH.DEFAULT_BASE_URL
issue_key = UPDATEBASEPATH.issue_key
load_env_file = UPDATEBASEPATH.load_env_file
normalize_basepath = UPDATEBASEPATH.normalize_basepath
put_issue_description = UPDATEBASEPATH.put_issue_description
update_description_adf = UPDATEBASEPATH.update_description_adf


DEFAULT_INPUT = "data/guess_missing_proxy/jira_missing_proxy_guess.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch update Jira issue description basepath from guess CSV.")
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--confident", type=int, default=80, help="Minimum confident percent, inclusive.")
    parser.add_argument("--apply", action="store_true", help="Actually update Jira. Default is dry-run.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum rows to process. Use 0 for no limit.")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between Jira updates.")
    parser.add_argument("--base-url", default=os.getenv("JIRA_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--email", default=os.getenv("JIRA_EMAIL"))
    parser.add_argument("--api-token", default=os.getenv("JIRA_API_TOKEN"))
    parser.add_argument("--env-file", default=".env")
    return parser.parse_args()


def confidence_value(value: str) -> int:
    cleaned = value.strip().rstrip("%")
    return int(cleaned) if cleaned.isdigit() else 0


def desc_basepath_value(value: str) -> str:
    cleaned = value.strip()
    if cleaned.lower().startswith("basepath:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    return normalize_basepath(cleaned)


def candidate_rows(path: str, min_confidence: int) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with open(path, newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        for row in reader:
            if confidence_value(row.get("confident", "")) < min_confidence:
                continue
            basepath = desc_basepath_value(row.get("desc_basepath", ""))
            card = row.get("card", "").strip()
            if not card or not basepath:
                continue
            rows.append({"card": card, "basepath": basepath, "confident": row.get("confident", "").strip()})
    return rows


def main() -> int:
    args = parse_args()
    load_env_file(args.env_file)
    email = args.email or os.getenv("JIRA_EMAIL")
    api_token = args.api_token or os.getenv("JIRA_API_TOKEN")
    base_url = args.base_url or os.getenv("JIRA_BASE_URL", DEFAULT_BASE_URL)
    if not email or not api_token:
        print("Missing JIRA_EMAIL or JIRA_API_TOKEN.", file=sys.stderr)
        return 2

    rows = candidate_rows(args.input, args.confident)
    if args.limit:
        rows = rows[: args.limit]

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode}: {len(rows)} row(s) match confident >= {args.confident}")
    client = JiraClient(JiraConfig(base_url, email, api_token))

    for row in rows:
        key = issue_key(row["card"])
        if not args.apply:
            print(f"Would update {key}: basepath: {row['basepath']} ({row['confident']})")
            continue

        issue = client.request_json("GET", f"/rest/api/3/issue/{key}?fields=description")
        description, action = update_description_adf(issue.get("fields", {}).get("description"), row["basepath"])
        put_issue_description(base_url, key, email, api_token, description)
        print(f"{action.capitalize()} {key}: basepath: {row['basepath']} ({row['confident']})")
        if args.sleep:
            time.sleep(args.sleep)

    if not args.apply:
        print("Dry-run only. Add --apply to update Jira.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
