#!/usr/bin/env python3
"""Add Apigee proxy names to latest-by-basepath CSV files."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any


DEFAULT_ORG = "gcp-pttep-th-it-apimgmt"
DEFAULT_INPUT_PREFIX = "jira_latest_by_basepath"
DEFAULT_OUTPUT_SUFFIX = "_with_proxy"
ENVS = ("dev", "qa", "uat", "prod")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Map Jira base paths to Apigee proxy names.")
    parser.add_argument("--org", default=DEFAULT_ORG, help="Apigee organization / GCP project id.")
    parser.add_argument("--input-prefix", default=DEFAULT_INPUT_PREFIX)
    parser.add_argument("--output-suffix", default=DEFAULT_OUTPUT_SUFFIX)
    parser.add_argument("--source", choices=("db", "api"), default="db")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--token", help="OAuth access token. Defaults to `gcloud auth print-access-token`.")
    parser.add_argument("--cache", default="apigee_basepath_proxy_map.csv")
    parser.add_argument("--use-cache", action="store_true", help="Skip Apigee API and reuse --cache.")
    return parser.parse_args()


def get_access_token(explicit_token: str | None) -> str:
    if explicit_token:
        return explicit_token

    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Cannot get gcloud access token: {details}")
    return result.stdout.strip()


def request_json(url: str, token: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Apigee API failed: {error.code} {error.reason}: {details}") from error


def request_bytes(url: str, token: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Apigee API failed: {error.code} {error.reason}: {details}") from error


def apigee_url(org: str, path: str) -> str:
    return f"https://apigee.googleapis.com/v1/organizations/{urllib.parse.quote(org)}/{path}"


def list_proxies(org: str, token: str) -> list[str]:
    data = request_json(apigee_url(org, "apis"), token)
    if isinstance(data, list):
        return [proxy_name(item) for item in data if proxy_name(item)]
    if isinstance(data, dict):
        return [proxy_name(item) for item in data.get("proxies", []) if proxy_name(item)]
    return []


def proxy_name(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("name", ""))
    return ""


def get_latest_revision(org: str, token: str, proxy: str) -> str | None:
    proxy_path = f"apis/{urllib.parse.quote(proxy, safe='')}"
    data = request_json(apigee_url(org, proxy_path), token)
    revisions = data.get("revision") or data.get("revisions") or []
    if not revisions:
        return None
    return max((revision_name(revision) for revision in revisions if revision_name(revision)), key=revision_key)


def revision_name(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return str(value.get("name") or value.get("revision") or "")
    return ""


def revision_key(value: str) -> tuple[int, str]:
    try:
        return (int(value), value)
    except ValueError:
        return (-1, value)


def get_revision_basepaths(org: str, token: str, proxy: str, revision: str) -> list[str]:
    base = f"apis/{urllib.parse.quote(proxy, safe='')}/revisions/{urllib.parse.quote(revision, safe='')}"
    revision_data = request_json(apigee_url(org, base), token)
    found = find_basepaths(revision_data)
    found.extend(get_revision_bundle_basepaths(org, token, proxy, revision))
    return unique_paths(found)


def get_revision_bundle_basepaths(org: str, token: str, proxy: str, revision: str) -> list[str]:
    base = f"apis/{urllib.parse.quote(proxy, safe='')}/revisions/{urllib.parse.quote(revision, safe='')}"
    url = apigee_url(org, base) + "?format=bundle"
    try:
        bundle = request_bytes(url, token)
    except RuntimeError as error:
        print(f"  skip bundle export for {proxy} rev {revision}: {error}", file=sys.stderr)
        return []

    found: list[str] = []
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml"):
                continue
            content = archive.read(name).decode("utf-8", errors="replace")
            found.extend(extract_xml_basepaths(content))
    return found


def extract_xml_basepaths(content: str) -> list[str]:
    return [
        normalize_basepath(match.group(1))
        for match in re.finditer(r"<BasePath>\s*([^<]+?)\s*</BasePath>", content, flags=re.IGNORECASE)
    ]


def find_basepaths(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_lower = key.lower()
            if key_lower in {"basepath", "basepaths", "base_path", "base_paths"}:
                found.extend(extract_path_values(child))
            else:
                found.extend(find_basepaths(child))
    elif isinstance(value, list):
        for item in value:
            found.extend(find_basepaths(item))
    return found


def extract_path_values(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = normalize_basepath(value)
        return [cleaned] if cleaned else []
    if isinstance(value, list):
        paths: list[str] = []
        for item in value:
            paths.extend(extract_path_values(item))
        return paths
    if isinstance(value, dict):
        return find_basepaths(value)
    return []


def normalize_basepath(value: str) -> str:
    cleaned = value.strip().rstrip("/")
    if not cleaned:
        return ""
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return cleaned or "/"


def unique_paths(paths: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for path in paths:
        normalized = normalize_basepath(path)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def build_proxy_map(org: str, token: str) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    proxies = list_proxies(org, token)
    print(f"Found {len(proxies)} Apigee proxy/proxies")

    for index, proxy in enumerate(proxies, start=1):
        print(f"[{index}/{len(proxies)}] {proxy}", file=sys.stderr)
        revision = get_latest_revision(org, token, proxy)
        if not revision:
            continue
        for basepath in get_revision_basepaths(org, token, proxy, revision):
            mapping.setdefault(basepath, []).append(proxy)

    return {basepath: sorted(set(proxies)) for basepath, proxies in mapping.items()}


def build_proxy_map_from_db(env_file: str) -> dict[str, list[str]]:
    env = load_env_file(env_file)
    db_url = env.get("APIGEE_SYNC_DB_URL") or os.getenv("APIGEE_SYNC_DB_URL", "")
    if not db_url:
        raise RuntimeError("Missing APIGEE_SYNC_DB_URL in env or .env")

    dsn = add_ssl_query_params(
        db_url,
        {
            "sslmode": "verify-ca",
            "sslrootcert": env.get("APIGEE_SYNC_DB_SSL_ROOTCERT") or os.getenv("APIGEE_SYNC_DB_SSL_ROOTCERT", ""),
            "sslcert": env.get("APIGEE_SYNC_DB_SSL_CERT") or os.getenv("APIGEE_SYNC_DB_SSL_CERT", ""),
            "sslkey": env.get("APIGEE_SYNC_DB_SSL_KEY") or os.getenv("APIGEE_SYNC_DB_SSL_KEY", ""),
        },
    )
    sql = """
        SELECT base_path, string_agg(DISTINCT proxy_name, ', ' ORDER BY proxy_name) AS proxy
        FROM apigee.apigee_proxy_endpoints
        WHERE COALESCE(base_path, '') <> ''
          AND COALESCE(proxy_name, '') <> ''
        GROUP BY base_path
        ORDER BY base_path;
    """
    result = subprocess.run(
        ["/opt/homebrew/bin/psql", dsn, "-At", "-F", "\t", "-c", sql],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Cannot query Apigee sync DB: {details}")

    mapping: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        basepath, proxy = (line.split("\t", 1) + [""])[:2]
        normalized = normalize_basepath(basepath)
        proxies = [item.strip() for item in proxy.split(",") if item.strip()]
        if normalized and proxies:
            mapping[normalized] = proxies

    print(f"Loaded {len(mapping)} basepath mapping row(s) from Apigee sync DB")
    return mapping


def load_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
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
            values[key.strip()] = value.strip().strip("'\"")
    return values


def add_ssl_query_params(db_url: str, params: dict[str, str]) -> str:
    active = {key: value for key, value in params.items() if value}
    if not active:
        return db_url
    separator = "&" if "?" in db_url else "?"
    query = urllib.parse.urlencode(active)
    return db_url + separator + query


def read_proxy_map(path: str) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    with open(path, newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        for row in reader:
            basepath = normalize_basepath(row.get("base path", ""))
            proxies = [item.strip() for item in row.get("proxy", "").split(",") if item.strip()]
            if basepath:
                mapping[basepath] = proxies
    return mapping


def write_proxy_map(path: str, mapping: dict[str, list[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=["base path", "proxy"])
        writer.writeheader()
        for basepath in sorted(mapping):
            writer.writerow({"base path": basepath, "proxy": ", ".join(mapping[basepath])})
    print(f"Wrote proxy map to {path}")


def add_proxy_to_env_files(input_prefix: str, output_suffix: str, proxy_map: dict[str, list[str]]) -> None:
    for env in ENVS:
        input_path = f"{input_prefix}_{env}.csv"
        output_path = f"{input_prefix}_{env}{output_suffix}.csv"
        count = 0
        with open(input_path, newline="", encoding="utf-8-sig") as source, open(
            output_path, "w", newline="", encoding="utf-8-sig"
        ) as output:
            reader = csv.DictReader(source)
            fieldnames = list(reader.fieldnames or [])
            if "proxy" not in fieldnames:
                fieldnames.append("proxy")
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            for row in reader:
                basepath = normalize_basepath(row.get("base path", ""))
                row["proxy"] = ", ".join(proxy_map.get(basepath, []))
                writer.writerow(row)
                count += 1
        print(f"Wrote {count} row(s) to {output_path}")


def main() -> int:
    args = parse_args()
    if args.use_cache:
        proxy_map = read_proxy_map(args.cache)
    elif args.source == "db":
        proxy_map = build_proxy_map_from_db(args.env_file)
        write_proxy_map(args.cache, proxy_map)
    else:
        token = get_access_token(args.token)
        proxy_map = build_proxy_map(args.org, token)
        write_proxy_map(args.cache, proxy_map)

    add_proxy_to_env_files(args.input_prefix, args.output_suffix, proxy_map)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
