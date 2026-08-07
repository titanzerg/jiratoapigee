#!/usr/bin/env python3
"""Add or update a Jira description basepath line."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from jira_export import JiraClient, JiraConfig, adf_to_text


DEFAULT_BASE_URL = "https://pttep.atlassian.net"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Add or update `basepath: ...` in a Jira issue description.")
    parser.add_argument("issue", help="Jira issue key or full URL, e.g. SOSP-32172")
    parser.add_argument("basepath", help='Basepath value, e.g. "/running_club"')
    parser.add_argument("--base-url", default=os.getenv("JIRA_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--email", default=os.getenv("JIRA_EMAIL"))
    parser.add_argument("--api-token", default=os.getenv("JIRA_API_TOKEN"))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--dry-run", action="store_true")
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


def normalize_basepath(value: str) -> str:
    cleaned = value.strip()
    if cleaned and not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return cleaned


def paragraph(text: str) -> dict[str, Any]:
    return {
        "type": "paragraph",
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ],
    }


def empty_doc() -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": []}


def update_description_adf(description: Any, basepath: str) -> tuple[dict[str, Any], str]:
    doc = description if isinstance(description, dict) else empty_doc()
    if doc.get("type") != "doc":
        doc = empty_doc()
    content = doc.setdefault("content", [])
    if not isinstance(content, list):
        content = []
        doc["content"] = content

    new_text = f"basepath: {basepath}"
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "paragraph":
            continue
        text = adf_to_text(block).strip().lower()
        if text.startswith("basepath:") or text.startswith("base path:"):
            block.clear()
            block.update(paragraph(new_text))
            return doc, "updated"

    if content and adf_to_text(content[-1]).strip():
        content.append(paragraph(""))
    content.append(paragraph(new_text))
    return doc, "added"


def put_issue_description(base_url: str, issue: str, email: str, api_token: str, description: dict[str, Any]) -> None:
    auth_client = JiraClient(JiraConfig(base_url, email, api_token))
    payload = json.dumps({"fields": {"description": description}}).encode("utf-8")
    request = urllib.request.Request(
        auth_client.base_url + f"/rest/api/3/issue/{issue}",
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": auth_client.auth_header,
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=60):
            return
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Jira API failed: {error.code} {error.reason}: {details}") from error


def main() -> int:
    args = parse_args()
    load_env_file(args.env_file)
    email = args.email or os.getenv("JIRA_EMAIL")
    api_token = args.api_token or os.getenv("JIRA_API_TOKEN")
    base_url = args.base_url or os.getenv("JIRA_BASE_URL", DEFAULT_BASE_URL)
    if not email or not api_token:
        print("Missing JIRA_EMAIL or JIRA_API_TOKEN.", file=sys.stderr)
        return 2

    key = issue_key(args.issue)
    basepath = normalize_basepath(args.basepath)
    client = JiraClient(JiraConfig(base_url, email, api_token))
    issue = client.request_json("GET", f"/rest/api/3/issue/{key}?fields=description")
    description, action = update_description_adf(issue.get("fields", {}).get("description"), basepath)

    if args.dry_run:
        print(f"Would {action} {key}: basepath: {basepath}")
        return 0

    put_issue_description(base_url, key, email, api_token, description)
    print(f"{action.capitalize()} {key}: basepath: {basepath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
