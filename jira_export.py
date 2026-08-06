#!/usr/bin/env python3
"""Export Jira API Support Bucket cards with Swagger/OpenAPI attachments to CSV."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable


DEFAULT_BASE_URL = "https://pttep.atlassian.net"
DEFAULT_PARENT = "SOSP-3"
DEFAULT_OUTPUT = "jira_api_support_export.csv"
DEFAULT_ISSUE_TYPES = ("Request", "Task")
SEARCH_FIELDS = ("summary", "description", "attachment", "created", "issuetype")
ATTACHMENT_EXTENSIONS = (".yaml", ".yml", ".json")


@dataclass(frozen=True)
class JiraConfig:
    base_url: str
    email: str
    api_token: str


class JiraClient:
    def __init__(self, config: JiraConfig) -> None:
        self.base_url = config.base_url.rstrip("/")
        auth = f"{config.email}:{config.api_token}".encode("utf-8")
        self.auth_header = "Basic " + base64.b64encode(auth).decode("ascii")

    def request_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url + path
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": self.auth_header,
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            raise JiraApiError(error.code, error.reason, details) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Cannot connect to Jira: {error.reason}") from error

    def search_issues(self, jql: str, page_size: int) -> Iterable[dict[str, Any]]:
        try:
            yield from self.search_issues_enhanced(jql, page_size)
        except JiraApiError as error:
            if error.status_code not in {404, 410}:
                raise
            yield from self.search_issues_legacy(jql, page_size)

    def search_issues_enhanced(self, jql: str, page_size: int) -> Iterable[dict[str, Any]]:
        path = "/rest/api/3/search/jql"
        next_page_token = None

        while True:
            body: dict[str, Any] = {
                "jql": jql,
                "fields": list(SEARCH_FIELDS),
                "maxResults": page_size,
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token

            data = self.request_json("POST", path, body)
            issues = data.get("issues", [])
            yield from issues

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

    def search_issues_legacy(self, jql: str, page_size: int) -> Iterable[dict[str, Any]]:
        path = "/rest/api/3/search"
        start_at = 0

        while True:
            body: dict[str, Any] = {
                "jql": jql,
                "fields": list(SEARCH_FIELDS),
                "maxResults": page_size,
                "startAt": start_at,
            }
            data = self.request_json("POST", path, body)
            issues = data.get("issues", [])
            yield from issues

            start_at += len(issues)
            total = data.get("total", 0)
            if not issues or start_at >= total:
                break

    def get_issue(self, issue_key: str) -> dict[str, Any]:
        fields = ",".join(SEARCH_FIELDS)
        return self.request_json("GET", f"/rest/api/3/issue/{issue_key}?fields={fields}")


class JiraApiError(RuntimeError):
    def __init__(self, status_code: int, reason: str, details: str) -> None:
        self.status_code = status_code
        super().__init__(f"Jira API failed: {status_code} {reason}: {details}")


def build_jql(parent: str, issue_types: Iterable[str]) -> str:
    quoted_types = ", ".join(quote_jql_value(issue_type) for issue_type in issue_types)
    return f"parent = {quote_jql_value(parent)} AND issuetype in ({quoted_types}) ORDER BY created DESC"


def quote_jql_value(value: str) -> str:
    if re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def adf_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (adf_to_text(item) for item in value)))
    if not isinstance(value, dict):
        return str(value)

    parts: list[str] = []
    if isinstance(value.get("text"), str):
        parts.append(value["text"])
    if value.get("type") == "hardBreak":
        parts.append("\n")
    if "attrs" in value:
        attrs = value["attrs"]
        if isinstance(attrs, dict):
            url = attrs.get("url") or attrs.get("href")
            if isinstance(url, str):
                parts.append(url)
    if "content" in value:
        child_text = adf_to_text(value["content"])
        if child_text:
            parts.append(child_text)
    return " ".join(part for part in parts if part).strip()


def has_swagger_attachment(issue: dict[str, Any]) -> bool:
    attachments = issue.get("fields", {}).get("attachment") or []
    for attachment in attachments:
        filename = str(attachment.get("filename", "")).lower()
        if filename.endswith(ATTACHMENT_EXTENSIONS):
            return True
    return False


def detect_environment(text: str, env: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.lower())
    terms = {
        "dev": ("dev", "development"),
        "qa": ("qa", "sit"),
        "uat": ("uat", "staging", "stage"),
        "prod": ("prod", "production"),
    }[env]

    for term in terms:
        pattern = rf"\b{re.escape(term)}\b"
        for match in re.finditer(pattern, normalized):
            window_start = max(0, match.start() - 30)
            window = normalized[window_start : match.end() + 30]
            if re.search(r"\b(no|not|ไม่|ไม่ต้อง|except|exclude|without)\b", window):
                continue
            return True
    return False


def extract_basepaths(text: str) -> list[str]:
    text = strip_markdown_link_targets(text)
    patterns = [
        r"\b(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?/\s*([a-z][\w{}.$~%+\-:,;@*]*(?:/[\w{}.$~%+\-:,;@*]+)+)",
        r"\bbase\s*path\b\s*(?:[:=\-]|is|คือ)?\s*([/\w{}.$~%+\-:,;@*]+)",
        r"\bbasepath\b\s*(?:[:=\-]|is|คือ)?\s*([/\w{}.$~%+\-:,;@*]+)",
        r"\bcontext\s*path\b\s*(?:[:=\-]|is|คือ)?\s*([/\w{}.$~%+\-:,;@*]+)",
        r"\bpath\b\s*(?:[:=\-]|is|คือ)?\s*([/\w{}.$~%+\-:,;@*]+)",
        r"https?://[^/\s)\]>'\"]+(/[\w{}.$~%+\-:,;@*/]+)",
        r"\b(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?(/[\w{}.$~%+\-:,;@*/]+)",
        r"(?<![\w.-])(/(?:api|apis|apigee|apim|v\d+|[a-z][\w-]+)(?:/[\w{}.$~%+\-:,;@*]+)+)",
    ]

    found: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            candidate = clean_basepath(match.group(1))
            if candidate and candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
    return found


def strip_markdown_link_targets(text: str) -> str:
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)


def clean_basepath(value: str) -> str:
    cleaned = value.strip().rstrip(".,;:)]}'\"")
    if cleaned and not cleaned.startswith("/") and "/" in cleaned:
        cleaned = "/" + cleaned
    if not cleaned.startswith("/"):
        return ""
    if cleaned in {"/", "//"}:
        return ""
    return cleaned


def issue_to_row(base_url: str, issue: dict[str, Any]) -> dict[str, Any]:
    fields = issue.get("fields", {})
    key = issue.get("key", "")
    title = fields.get("summary") or ""
    description = adf_to_text(fields.get("description"))
    searchable_text = f"{title}\n{description}"

    return {
        "link": f"{base_url.rstrip('/')}/browse/{key}",
        "title": title,
        "dev": detect_environment(searchable_text, "dev"),
        "qa": detect_environment(searchable_text, "qa"),
        "uat": detect_environment(searchable_text, "uat"),
        "prod": detect_environment(searchable_text, "prod"),
        "basepath": ", ".join(extract_basepaths(searchable_text)),
        "create date": fields.get("created") or "",
    }


def write_csv(path: str, rows: Iterable[dict[str, Any]]) -> int:
    fieldnames = ["link", "title", "dev", "qa", "uat", "prod", "basepath", "create date"]
    count = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: format_csv_value(value) for key, value in row.items()})
            count += 1
    return count


def format_csv_value(value: Any) -> Any:
    if isinstance(value, bool):
        return str(value).lower()
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export SOSP Jira Request/Task cards with .yaml/.yml/.json attachments to CSV."
    )
    parser.add_argument("--base-url", default=os.getenv("JIRA_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--email", default=os.getenv("JIRA_EMAIL"))
    parser.add_argument("--api-token", default=os.getenv("JIRA_API_TOKEN"))
    parser.add_argument("--parent", default=DEFAULT_PARENT)
    parser.add_argument(
        "--issue-types",
        default=",".join(DEFAULT_ISSUE_TYPES),
        help="Comma-separated Jira issue types. Default: Request,Task",
    )
    parser.add_argument("--jql", help="Override generated JQL completely.")
    parser.add_argument("--issue-key", help="Export one Jira issue key, useful for debugging basepath extraction.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--limit", type=int, default=10, help="Maximum matching cards to export. Use 0 for no limit.")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between matching rows.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.email or not args.api_token:
        print("Missing JIRA_EMAIL or JIRA_API_TOKEN.", file=sys.stderr)
        return 2

    issue_types = [value.strip() for value in args.issue_types.split(",") if value.strip()]
    jql = args.jql or build_jql(args.parent, issue_types)
    client = JiraClient(JiraConfig(args.base_url, args.email, args.api_token))

    if args.issue_key:
        issue = client.get_issue(args.issue_key)
        if not has_swagger_attachment(issue):
            print(f"{args.issue_key} has no .yaml/.yml/.json attachment; exporting it anyway for debugging.")
        count = write_csv(args.output, [issue_to_row(args.base_url, issue)])
        print(f"Exported {count} Jira issue(s) to {args.output}")
        return 0

    def rows() -> Iterable[dict[str, Any]]:
        matched = 0
        for issue in client.search_issues(jql, args.page_size):
            if has_swagger_attachment(issue):
                yield issue_to_row(args.base_url, issue)
                matched += 1
                if args.limit and matched >= args.limit:
                    break
                if args.sleep:
                    time.sleep(args.sleep)

    count = write_csv(args.output, rows())
    print(f"Exported {count} Jira issue(s) to {args.output}")
    print(f"JQL: {jql}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
