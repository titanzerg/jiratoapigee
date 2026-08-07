#!/usr/bin/env python3
"""Export Jira API Support Bucket cards with Swagger/OpenAPI attachments to CSV."""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Any, Iterable

from add_apigee_proxy_name import (
    DEFAULT_BUNDLE_CACHE_DIR,
    DEFAULT_ORG,
    DEFAULT_SWAGGER_CACHE_DIR,
    ProxyInfo,
    build_proxy_info_from_db,
    compare_path,
    extract_condition_paths,
    extract_xml_basepaths,
    get_access_token,
    get_jira_swagger_paths,
    load_env_file,
    normalize_basepath as normalize_apigee_basepath,
    read_or_download_revision_bundle,
    remove_basepath_prefix,
)


DEFAULT_BASE_URL = "https://pttep.atlassian.net"
DEFAULT_PARENT = "SOSP-3"
DEFAULT_OUTPUT = "data/jira_export/jira_api_support_export.csv"
DEFAULT_ISSUE_TYPES = ("Request", "Task")
SEARCH_FIELDS = ("summary", "description", "attachment", "created", "issuetype")
ATTACHMENT_EXTENSIONS = (".yaml", ".yml", ".json")


@dataclass
class ConfidenceContext:
    org: str
    env_file: str
    token: str
    bundle_cache_dir: str
    swagger_cache_dir: str
    proxy_info_by_basepath: dict[str, list[ProxyInfo]]
    swagger_cache: dict[str, list[str]]
    flow_cache: dict[tuple[str, str, str], set[str]]


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
    text = re.sub(r"/https?://[^\s)\]>'\"]+", " ", text, flags=re.IGNORECASE)
    patterns = [
        r"(?<!/)\b(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?/\s*([a-z][\w{}.$~%+\-:,;@*]*(?:/[\w{}.$~%+\-:,;@*]+)+)",
        r"\bbase\s*path\b\s*(?:[:=\-]|is|คือ)?\s*([/\w{}.$~%+\-:,;@*]+)",
        r"\bbasepath\b\s*(?:[:=\-]|is|คือ)?\s*([/\w{}.$~%+\-:,;@*]+)",
        r"\bcontext\s*path\b\s*(?:[:=\-]|is|คือ)?\s*([/\w{}.$~%+\-:,;@*]+)",
        r"\bpath\b\s*(?:[:=\-]|is|คือ)?\s*([/\w{}.$~%+\-:,;@*]+)",
        r"(?<!/)https?://[^/\s)\]>'\"]+(/[\w{}.$~%+\-:,;@*/]+)",
        r"(?<!/)\b(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?(/[\w{}.$~%+\-:,;@*/]+)",
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


def extract_explicit_basepaths(text: str) -> list[str]:
    matches = list(re.finditer(
        r"\bbase\s*path\b\s*:\s*([^\n\r]+)|\bbasepath\b\s*:\s*([^\n\r]+)",
        text,
        flags=re.IGNORECASE,
    ))
    if not matches:
        return []

    raw_value = matches[-1].group(1) or matches[-1].group(2) or ""
    found: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,;]", raw_value):
        candidate = clean_basepath(item)
        if candidate and candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
    return found


def strip_markdown_link_targets(text: str) -> str:
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)


def clean_basepath(value: str) -> str:
    cleaned = value.strip().rstrip(".,;:)]}'\"")
    if re.match(r"^/https?://", cleaned, flags=re.IGNORECASE):
        return ""
    if cleaned and not cleaned.startswith("/") and "/" in cleaned:
        cleaned = "/" + cleaned
    if not cleaned.startswith("/"):
        return ""
    if cleaned in {"/", "//"}:
        return ""
    return cleaned


def issue_to_row(
    base_url: str,
    issue: dict[str, Any],
    confidence_context: ConfidenceContext | None = None,
) -> dict[str, Any]:
    fields = issue.get("fields", {})
    key = issue.get("key", "")
    title = fields.get("summary") or ""
    description = adf_to_text(fields.get("description"))
    searchable_text = f"{title}\n{description}"
    explicit_basepaths = extract_explicit_basepaths(description)
    basepaths = explicit_basepaths or extract_basepaths(searchable_text)
    card_url = f"{base_url.rstrip('/')}/browse/{key}"

    return {
        "link": card_url,
        "title": title,
        "dev": detect_environment(searchable_text, "dev"),
        "qa": detect_environment(searchable_text, "qa"),
        "uat": detect_environment(searchable_text, "uat"),
        "prod": detect_environment(searchable_text, "prod"),
        "basepath": ", ".join(basepaths),
        "confident": issue_confidence(card_url, basepaths, confidence_context),
        "create date": fields.get("created") or "",
    }


def create_confidence_context(args: argparse.Namespace) -> ConfidenceContext | None:
    if args.skip_confident:
        return None

    try:
        proxy_info_by_basepath = build_proxy_info_from_db(args.env_file)
    except Exception as error:
        print(f"warning: cannot load Apigee basepath map; confident will be blank: {error}", file=sys.stderr)
        return None

    try:
        token = get_access_token(args.token)
    except RuntimeError as error:
        print(f"warning: {error}", file=sys.stderr)
        print("warning: continuing with existing Apigee bundle cache only", file=sys.stderr)
        token = ""

    return ConfidenceContext(
        org=args.org,
        env_file=args.env_file,
        token=token,
        bundle_cache_dir=args.bundle_cache_dir,
        swagger_cache_dir=args.swagger_cache_dir,
        proxy_info_by_basepath=proxy_info_by_basepath,
        swagger_cache={},
        flow_cache={},
    )


def issue_confidence(card_url: str, basepaths: list[str], context: ConfidenceContext | None) -> str:
    if not context or not basepaths:
        return ""

    try:
        swagger_paths = get_jira_swagger_paths(
            card_url,
            context.env_file,
            context.swagger_cache,
            context.swagger_cache_dir,
        )
    except Exception as error:
        print(f"warning: cannot read swagger paths for {card_url}; confident will be blank: {error}", file=sys.stderr)
        return ""

    if not swagger_paths:
        return ""

    scores: list[tuple[str, str]] = []
    seen: set[str] = set()
    for basepath in basepaths:
        normalized = normalize_apigee_basepath(basepath)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        flow_paths = get_basepath_flow_paths(context, normalized)
        score = score_confidence(swagger_paths, normalized, flow_paths)
        if score:
            scores.append((normalized, score))

    if len(basepaths) == 1:
        return scores[0][1] if scores else ""
    return ", ".join(f"{basepath}={score}" for basepath, score in scores)


def get_basepath_flow_paths(context: ConfidenceContext, basepath: str) -> set[str]:
    flow_paths: set[str] = set()
    for info in context.proxy_info_by_basepath.get(basepath, []):
        key = (info.proxy, info.revision, basepath)
        if key not in context.flow_cache:
            context.flow_cache[key] = read_basepath_flow_paths(context, info, basepath)
        flow_paths.update(context.flow_cache[key])
    return flow_paths


def read_basepath_flow_paths(context: ConfidenceContext, info: ProxyInfo, basepath: str) -> set[str]:
    bundle = read_or_download_revision_bundle(
        context.org,
        context.token,
        info.proxy,
        info.revision,
        context.bundle_cache_dir,
    )
    if not bundle:
        return set()

    paths: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml") or "/proxies/" not in name:
                continue
            content = archive.read(name).decode("utf-8", errors="replace")
            xml_basepaths = extract_xml_basepaths(content)
            if not any(normalize_apigee_basepath(item) == basepath for item in xml_basepaths):
                continue
            paths.update(compare_path(path) for path in extract_condition_paths(content) if compare_path(path))
    return paths


def score_confidence(swagger_paths: list[str], basepath: str, flow_paths: set[str]) -> str:
    comparable = {
        compare_path(remove_basepath_prefix(path, basepath))
        for path in swagger_paths
        if compare_path(remove_basepath_prefix(path, basepath))
    }
    if not comparable or not flow_paths:
        return ""
    matched = comparable & flow_paths
    return f"{round((len(matched) / len(comparable)) * 100)}%"


def write_csv(path: str, rows: Iterable[dict[str, Any]]) -> int:
    fieldnames = ["link", "title", "dev", "qa", "uat", "prod", "basepath", "confident", "create date"]
    count = 0
    ensure_parent_dir(path)
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


def ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


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
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--org", default=os.getenv("APIGEE_ORG"), help="Apigee organization / GCP project id.")
    parser.add_argument("--token", help="OAuth access token. Defaults to `gcloud auth print-access-token`.")
    parser.add_argument("--bundle-cache-dir", default=DEFAULT_BUNDLE_CACHE_DIR)
    parser.add_argument("--swagger-cache-dir", default=DEFAULT_SWAGGER_CACHE_DIR)
    parser.add_argument("--skip-confident", action="store_true", help="Do not calculate Apigee flow confidence.")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--limit", type=int, default=0, help="Maximum matching cards to export. Use 0 for no limit.")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between matching rows.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env = load_env_file(args.env_file)
    args.base_url = args.base_url or env.get("JIRA_BASE_URL") or DEFAULT_BASE_URL
    args.email = args.email or env.get("JIRA_EMAIL")
    args.api_token = args.api_token or env.get("JIRA_API_TOKEN")
    args.org = args.org or env.get("APIGEE_ORG") or DEFAULT_ORG
    if not args.email or not args.api_token:
        print("Missing JIRA_EMAIL or JIRA_API_TOKEN.", file=sys.stderr)
        return 2

    issue_types = [value.strip() for value in args.issue_types.split(",") if value.strip()]
    jql = args.jql or build_jql(args.parent, issue_types)
    client = JiraClient(JiraConfig(args.base_url, args.email, args.api_token))
    confidence_context = create_confidence_context(args)

    if args.issue_key:
        issue = client.get_issue(args.issue_key)
        if not has_swagger_attachment(issue):
            print(f"{args.issue_key} has no .yaml/.yml/.json attachment; exporting it anyway for debugging.")
        count = write_csv(args.output, [issue_to_row(args.base_url, issue, confidence_context)])
        print(f"Exported {count} Jira issue(s) to {args.output}")
        return 0

    def rows() -> Iterable[dict[str, Any]]:
        matched = 0
        for issue in client.search_issues(jql, args.page_size):
            if has_swagger_attachment(issue):
                yield issue_to_row(args.base_url, issue, confidence_context)
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
