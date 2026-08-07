#!/usr/bin/env python3
"""Print Jira issue description as plain text."""

from __future__ import annotations

import argparse
import os
import sys

from jira_export import JiraClient, JiraConfig, adf_to_text


DEFAULT_BASE_URL = "https://pttep.atlassian.net"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print Jira issue description.")
    parser.add_argument("issue", help="Jira issue key or full Jira issue URL, e.g. SOSP-32310")
    parser.add_argument("--base-url", default=os.getenv("JIRA_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--email", default=os.getenv("JIRA_EMAIL"))
    parser.add_argument("--api-token", default=os.getenv("JIRA_API_TOKEN"))
    parser.add_argument("--env-file", default=".env")
    return parser.parse_args()


def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as source:
        for raw_line in source:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def issue_key(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1].strip()


def main() -> int:
    args = parse_args()
    load_env_file(args.env_file)
    email = args.email or os.getenv("JIRA_EMAIL")
    api_token = args.api_token or os.getenv("JIRA_API_TOKEN")
    base_url = args.base_url or os.getenv("JIRA_BASE_URL", DEFAULT_BASE_URL)
    if not email or not api_token:
        print("Missing JIRA_EMAIL or JIRA_API_TOKEN.", file=sys.stderr)
        return 2

    client = JiraClient(JiraConfig(base_url, email, api_token))
    issue = client.request_json("GET", f"/rest/api/3/issue/{issue_key(args.issue)}?fields=description")
    print(adf_to_text(issue.get("fields", {}).get("description")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
