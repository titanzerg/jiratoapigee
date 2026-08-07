#!/usr/bin/env python3
"""Export deployed Apigee proxy basepaths and matching Jira cards to CSV."""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import urllib.parse
import zipfile
from dataclasses import dataclass
from typing import Any

from add_apigee_proxy_name import (
    DEFAULT_BUNDLE_CACHE_DIR,
    DEFAULT_ORG,
    DEFAULT_SWAGGER_CACHE_DIR,
    apigee_url,
    compare_path,
    extract_condition_paths,
    extract_xml_basepaths,
    get_access_token,
    get_jira_swagger_paths,
    normalize_basepath,
    read_or_download_revision_bundle,
    request_json,
)
from guess_missing_proxy import score_basepath


DEFAULT_INPUT = "data/add_apigee_proxy_name/jira_latest_by_basepath_uat_with_proxy.csv"
DEFAULT_OUTPUT = "data/export_deployed_apigee_proxies/apigee_deployed_proxy_basepaths.csv"


@dataclass(frozen=True)
class Deployment:
    proxy: str
    revision: str


@dataclass
class CardMatch:
    card: str = ""
    proxy: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export deployed Apigee proxy basepaths with mapped Jira cards.")
    parser.add_argument("--org", default=os.getenv("APIGEE_ORG", DEFAULT_ORG))
    parser.add_argument("--token", help="OAuth access token. Defaults to `gcloud auth print-access-token`.")
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--bundle-cache-dir", default=DEFAULT_BUNDLE_CACHE_DIR)
    parser.add_argument("--swagger-cache-dir", default=DEFAULT_SWAGGER_CACHE_DIR)
    parser.add_argument("--limit", type=int, default=0, help="Maximum deployed proxy revisions to process. Use 0 for all.")
    return parser.parse_args()


def list_deployed_revisions(org: str, token: str) -> list[Deployment]:
    data = request_json(apigee_url(org, "deployments"), token)
    deployments = extract_deployments(data)
    seen: set[tuple[str, str]] = set()
    result: list[Deployment] = []
    for proxy, revision in deployments:
        key = (proxy, revision)
        if not proxy or not revision or key in seen:
            continue
        seen.add(key)
        result.append(Deployment(proxy=proxy, revision=revision))
    return sorted(result, key=lambda item: (item.proxy.lower(), revision_sort_key(item.revision)))


def extract_deployments(value: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        proxy = deployment_proxy_name(value)
        revision = deployment_revision(value)
        if proxy and revision:
            found.append((proxy, revision))
        for child in value.values():
            found.extend(extract_deployments(child))
    elif isinstance(value, list):
        for item in value:
            found.extend(extract_deployments(item))
    return found


def deployment_proxy_name(value: dict[str, Any]) -> str:
    for key in ("apiProxy", "apiProxyName", "proxy", "proxyName", "name"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            if "/apis/" in candidate:
                return urllib.parse.unquote(candidate.rstrip("/").rsplit("/", 1)[-1])
            return candidate
    return ""


def deployment_revision(value: dict[str, Any]) -> str:
    for key in ("revision", "revisionName"):
        candidate = value.get(key)
        if isinstance(candidate, (str, int)) and str(candidate):
            return str(candidate)
    name = value.get("name")
    if isinstance(name, str) and "/revisions/" in name:
        return urllib.parse.unquote(name.rstrip("/").rsplit("/", 1)[-1])
    return ""


def revision_sort_key(value: str) -> tuple[int, str]:
    try:
        return (int(value), value)
    except ValueError:
        return (-1, value)


def read_card_matches(path: str) -> tuple[dict[tuple[str, str], CardMatch], dict[str, CardMatch]]:
    by_proxy_basepath: dict[tuple[str, str], CardMatch] = {}
    by_basepath: dict[str, CardMatch] = {}
    with open(path, newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        for row in reader:
            basepath = normalize_basepath(row.get("base path", ""))
            card = row.get("card", "").strip()
            proxies = [item.strip() for item in row.get("proxy", "").split(",") if item.strip()]
            if not basepath or not card:
                continue
            if basepath not in by_basepath:
                by_basepath[basepath] = CardMatch(card=card, proxy=", ".join(proxies))
            for proxy in proxies:
                by_proxy_basepath[(proxy, basepath)] = CardMatch(card=card, proxy=proxy)
    return by_proxy_basepath, by_basepath


def read_basepath_flows(org: str, token: str, deployment: Deployment, bundle_cache_dir: str) -> dict[str, set[str]]:
    bundle = read_or_download_revision_bundle(org, token, deployment.proxy, deployment.revision, bundle_cache_dir)
    if not bundle:
        return {}

    result: dict[str, set[str]] = {}
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml") or "/proxies/" not in name:
                continue
            content = archive.read(name).decode("utf-8", errors="replace")
            basepaths = extract_xml_basepaths(content)
            if not basepaths:
                continue
            flow_paths = {compare_path(path) for path in extract_condition_paths(content) if compare_path(path)}
            for basepath in basepaths:
                normalized = normalize_basepath(basepath)
                if normalized:
                    result.setdefault(normalized, set()).update(flow_paths)
    return result


def match_card(
    proxy: str,
    basepath: str,
    by_proxy_basepath: dict[tuple[str, str], CardMatch],
    by_basepath: dict[str, CardMatch],
) -> CardMatch:
    return by_proxy_basepath.get((proxy, basepath)) or by_basepath.get(basepath) or CardMatch()


def calculate_confident(
    card: str,
    basepath: str,
    flow_paths: set[str],
    env_file: str,
    swagger_cache_dir: str,
    swagger_cache: dict[str, list[str]],
) -> str:
    if not card or not flow_paths:
        return ""
    swagger_paths = get_jira_swagger_paths(card, env_file, swagger_cache, swagger_cache_dir)
    if not swagger_paths:
        return ""
    confidence = score_basepath(swagger_paths, basepath, flow_paths)[0]
    return f"{confidence}%" if confidence > 0 else ""


def ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def write_output(path: str, rows: list[dict[str, str]]) -> None:
    ensure_parent_dir(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=["proxy_name", "basepath", "card", "confident"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    args = parse_args()
    token = get_access_token(args.token)
    by_proxy_basepath, by_basepath = read_card_matches(args.input)
    deployments = list_deployed_revisions(args.org, token)
    if args.limit:
        deployments = deployments[: args.limit]
    print(f"Found {len(deployments)} deployed proxy revision(s)")

    rows: list[dict[str, str]] = []
    seen_rows: set[tuple[str, str]] = set()
    swagger_cache: dict[str, list[str]] = {}
    for index, deployment in enumerate(deployments, start=1):
        if index % 25 == 0:
            print(f"processed {index}/{len(deployments)} deployed revision(s)", file=sys.stderr)
        basepath_flows = read_basepath_flows(args.org, token, deployment, args.bundle_cache_dir)
        for basepath in sorted(basepath_flows):
            row_key = (deployment.proxy, basepath)
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)
            match = match_card(deployment.proxy, basepath, by_proxy_basepath, by_basepath)
            confident = calculate_confident(
                match.card,
                basepath,
                basepath_flows[basepath],
                args.env_file,
                args.swagger_cache_dir,
                swagger_cache,
            )
            rows.append(
                {
                    "proxy_name": deployment.proxy,
                    "basepath": basepath,
                    "card": match.card,
                    "confident": confident,
                }
            )

    rows.sort(key=lambda row: (row["proxy_name"].lower(), row["basepath"].lower()))
    write_output(args.output, rows)
    print(f"Wrote {len(rows)} row(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
