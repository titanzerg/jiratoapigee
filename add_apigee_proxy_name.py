#!/usr/bin/env python3
"""Add Apigee proxy names to latest-by-basepath CSV files."""

from __future__ import annotations

import argparse
import base64
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
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Any


DEFAULT_ORG = "gcp-pttep-th-it-apimgmt"
DEFAULT_INPUT_PREFIX = "data/summarize_latest_by_env/jira_latest_by_basepath"
DEFAULT_OUTPUT_PREFIX = "data/add_apigee_proxy_name/jira_latest_by_basepath"
DEFAULT_OUTPUT_SUFFIX = "_with_proxy"
DEFAULT_BUNDLE_CACHE_DIR = "data/add_apigee_proxy_name/apigee_bundle_cache"
DEFAULT_SWAGGER_CACHE_DIR = "data/add_apigee_proxy_name/jira_swagger_path_cache"
ENVS = ("dev", "qa", "uat", "prod")


@dataclass(frozen=True)
class ProxyInfo:
    proxy: str
    revision: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Map Jira base paths to Apigee proxy names.")
    parser.add_argument("--org", default=DEFAULT_ORG, help="Apigee organization / GCP project id.")
    parser.add_argument("--input-prefix", default=DEFAULT_INPUT_PREFIX)
    parser.add_argument("--output-prefix", default=DEFAULT_OUTPUT_PREFIX)
    parser.add_argument("--output-suffix", default=DEFAULT_OUTPUT_SUFFIX)
    parser.add_argument("--source", choices=("db", "api"), default="db")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--token", help="OAuth access token. Defaults to `gcloud auth print-access-token`.")
    parser.add_argument("--cache", default="data/add_apigee_proxy_name/apigee_basepath_proxy_map.csv")
    parser.add_argument("--bundle-cache-dir", default=DEFAULT_BUNDLE_CACHE_DIR)
    parser.add_argument("--swagger-cache-dir", default=DEFAULT_SWAGGER_CACHE_DIR)
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


def request_json_basic(url: str, email: str, api_token: str) -> Any:
    auth = f"{email}:{api_token}".encode("utf-8")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": "Basic " + base64.b64encode(auth).decode("ascii"),
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def request_bytes_basic(url: str, email: str, api_token: str) -> bytes:
    auth = f"{email}:{api_token}".encode("utf-8")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "Authorization": "Basic " + base64.b64encode(auth).decode("ascii"),
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


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


def get_revision_basepaths(org: str, token: str, proxy: str, revision: str, bundle_cache_dir: str) -> list[str]:
    base = f"apis/{urllib.parse.quote(proxy, safe='')}/revisions/{urllib.parse.quote(revision, safe='')}"
    revision_data = request_json(apigee_url(org, base), token)
    found = find_basepaths(revision_data)
    found.extend(get_revision_bundle_basepaths(org, token, proxy, revision, bundle_cache_dir))
    return unique_paths(found)


def get_revision_bundle_basepaths(org: str, token: str, proxy: str, revision: str, bundle_cache_dir: str) -> list[str]:
    bundle = read_or_download_revision_bundle(org, token, proxy, revision, bundle_cache_dir)
    if not bundle:
        return []

    found: list[str] = []
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml"):
                continue
            content = archive.read(name).decode("utf-8", errors="replace")
            found.extend(extract_xml_basepaths(content))
    return found


def get_revision_bundle_flow_paths(org: str, token: str, proxy: str, revision: str, bundle_cache_dir: str) -> list[str]:
    bundle = read_or_download_revision_bundle(org, token, proxy, revision, bundle_cache_dir)
    if not bundle:
        return []

    paths: list[str] = []
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml"):
                continue
            content = archive.read(name).decode("utf-8", errors="replace")
            paths.extend(extract_condition_paths(content))
    return unique_paths(paths)


def read_or_download_revision_bundle(
    org: str,
    token: str,
    proxy: str,
    revision: str,
    bundle_cache_dir: str,
) -> bytes:
    os.makedirs(bundle_cache_dir, exist_ok=True)
    cache_path = bundle_cache_path(bundle_cache_dir, proxy, revision)
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as source:
            return source.read()

    base = f"apis/{urllib.parse.quote(proxy, safe='')}/revisions/{urllib.parse.quote(revision, safe='')}"
    url = apigee_url(org, base) + "?format=bundle"
    try:
        bundle = request_bytes(url, token)
    except RuntimeError as error:
        print(f"  skip bundle export for {proxy} rev {revision}: {error}", file=sys.stderr)
        return b""

    with open(cache_path, "wb") as output:
        output.write(bundle)
    return bundle


def bundle_cache_path(bundle_cache_dir: str, proxy: str, revision: str) -> str:
    safe_proxy = re.sub(r"[^A-Za-z0-9._-]+", "_", proxy)
    safe_revision = re.sub(r"[^A-Za-z0-9._-]+", "_", revision)
    return os.path.join(bundle_cache_dir, f"{safe_proxy}__rev_{safe_revision}.zip")


def extract_xml_basepaths(content: str) -> list[str]:
    return [
        normalize_basepath(match.group(1))
        for match in re.finditer(r"<BasePath>\s*([^<]+?)\s*</BasePath>", content, flags=re.IGNORECASE)
    ]


def extract_condition_paths(content: str) -> list[str]:
    paths: list[str] = []
    try:
        root = ET.fromstring(content)
        conditions = [node.text or "" for node in root.iter() if strip_namespace(node.tag).lower() == "condition"]
    except ET.ParseError:
        conditions = re.findall(r"<Condition>\s*(.*?)\s*</Condition>", content, flags=re.IGNORECASE | re.DOTALL)

    for condition in conditions:
        decoded = html_unescape(condition)
        patterns = [
            r"MatchesPath\s*\(?\s*[\"']([^\"']+)[\"']",
            r"proxy\.pathsuffix\s*(?:==|=|Equals)\s*[\"']([^\"']+)[\"']",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, decoded, flags=re.IGNORECASE):
                path = normalize_flow_path(match.group(1))
                if path:
                    paths.append(path)
    return unique_paths(paths)


def strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def html_unescape(value: str) -> str:
    return (
        value.replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )


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


def normalize_flow_path(value: str) -> str:
    cleaned = value.strip().strip("\"'").split("?", 1)[0].rstrip("/")
    cleaned = re.sub(r"\*\*|\*", "{var}", cleaned)
    cleaned = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"{\1}", cleaned)
    if not cleaned or cleaned == "{var}":
        return ""
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return cleaned or "/"


def compare_path(value: str) -> str:
    cleaned = normalize_flow_path(value).lower()
    cleaned = re.sub(r"\{[^}/]+\}", "{}", cleaned)
    return cleaned


def remove_basepath_prefix(path: str, basepath: str) -> str:
    normalized_path = normalize_flow_path(path)
    normalized_basepath = normalize_basepath(basepath)
    if normalized_basepath != "/" and normalized_path.lower().startswith((normalized_basepath + "/").lower()):
        return normalized_path[len(normalized_basepath) :]
    if normalized_path.lower() == normalized_basepath.lower():
        return "/"
    return normalized_path


def unique_paths(paths: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for path in paths:
        normalized = normalize_basepath(path)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def build_proxy_map(org: str, token: str, bundle_cache_dir: str) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    proxies = list_proxies(org, token)
    print(f"Found {len(proxies)} Apigee proxy/proxies")

    for index, proxy in enumerate(proxies, start=1):
        print(f"[{index}/{len(proxies)}] {proxy}", file=sys.stderr)
        revision = get_latest_revision(org, token, proxy)
        if not revision:
            continue
        for basepath in get_revision_basepaths(org, token, proxy, revision, bundle_cache_dir):
            mapping.setdefault(basepath, []).append(proxy)

    return {basepath: sorted(set(proxies)) for basepath, proxies in mapping.items()}


def build_proxy_map_from_db(env_file: str) -> dict[str, list[str]]:
    proxy_info = build_proxy_info_from_db(env_file)
    mapping = {basepath: [item.proxy for item in infos] for basepath, infos in proxy_info.items()}
    print(f"Loaded {len(mapping)} basepath mapping row(s) from Apigee sync DB")
    return mapping


def build_proxy_info_from_db(env_file: str) -> dict[str, list[ProxyInfo]]:
    dsn = build_db_dsn(env_file)
    sql = """
        SELECT base_path, proxy_name, max(revision)::text AS revision
        FROM apigee.apigee_proxy_endpoints
        WHERE COALESCE(base_path, '') <> ''
          AND COALESCE(proxy_name, '') <> ''
          AND revision IS NOT NULL
        GROUP BY base_path, proxy_name
        ORDER BY base_path, proxy_name;
    """
    result = subprocess.run(
        ["/opt/homebrew/bin/psql", dsn, "-At", "-F", "\t", "-c", sql],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Cannot query Apigee sync DB: {details}")

    mapping: dict[str, list[ProxyInfo]] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        basepath, proxy, revision = (line.split("\t") + ["", "", ""])[:3]
        normalized = normalize_basepath(basepath)
        if normalized and proxy and revision:
            mapping.setdefault(normalized, []).append(ProxyInfo(proxy=proxy, revision=revision))

    print(f"Loaded {len(mapping)} basepath/proxy/revision row(s) from Apigee sync DB")
    return mapping


def build_db_dsn(env_file: str) -> str:
    env = load_env_file(env_file)
    db_url = env.get("APIGEE_SYNC_DB_URL") or os.getenv("APIGEE_SYNC_DB_URL", "")
    if not db_url:
        raise RuntimeError("Missing APIGEE_SYNC_DB_URL in env or .env")

    return add_ssl_query_params(
        db_url,
        {
            "sslmode": "verify-ca",
            "sslrootcert": env.get("APIGEE_SYNC_DB_SSL_ROOTCERT") or os.getenv("APIGEE_SYNC_DB_SSL_ROOTCERT", ""),
            "sslcert": env.get("APIGEE_SYNC_DB_SSL_CERT") or os.getenv("APIGEE_SYNC_DB_SSL_CERT", ""),
            "sslkey": env.get("APIGEE_SYNC_DB_SSL_KEY") or os.getenv("APIGEE_SYNC_DB_SSL_KEY", ""),
        },
    )


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


def ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def get_jira_swagger_paths(
    card_url: str,
    env_file: str,
    memory_cache: dict[str, list[str]],
    swagger_cache_dir: str,
) -> list[str]:
    issue_key = card_url.rstrip("/").rsplit("/", 1)[-1]
    if not issue_key:
        return []
    if issue_key in memory_cache:
        return memory_cache[issue_key]

    os.makedirs(swagger_cache_dir, exist_ok=True)
    cache_path = os.path.join(swagger_cache_dir, f"{issue_key}.json")
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as source:
            paths = json.load(source)
        memory_cache[issue_key] = paths if isinstance(paths, list) else []
        return memory_cache[issue_key]

    env = load_env_file(env_file)
    base_url = (env.get("JIRA_BASE_URL") or os.getenv("JIRA_BASE_URL", "")).rstrip("/")
    email = env.get("JIRA_EMAIL") or os.getenv("JIRA_EMAIL", "")
    api_token = env.get("JIRA_API_TOKEN") or os.getenv("JIRA_API_TOKEN", "")
    if not base_url or not email or not api_token:
        memory_cache[issue_key] = []
        return []

    try:
        issue = request_json_basic(f"{base_url}/rest/api/3/issue/{issue_key}?fields=attachment", email, api_token)
    except Exception as error:
        print(f"  skip Jira issue {issue_key}: {error}", file=sys.stderr)
        memory_cache[issue_key] = []
        return []

    paths: list[str] = []
    for attachment in issue.get("fields", {}).get("attachment") or []:
        filename = str(attachment.get("filename", "")).lower()
        if not filename.endswith((".json", ".yaml", ".yml")):
            continue
        content_url = attachment.get("content")
        if not content_url:
            continue
        try:
            content = request_bytes_basic(content_url, email, api_token).decode("utf-8", errors="replace")
        except Exception as error:
            print(f"  skip Jira attachment {issue_key}/{filename}: {error}", file=sys.stderr)
            continue
        paths.extend(extract_swagger_paths(content, filename))

    memory_cache[issue_key] = unique_paths(paths)
    with open(cache_path, "w", encoding="utf-8") as output:
        json.dump(memory_cache[issue_key], output, ensure_ascii=False, indent=2)
    return memory_cache[issue_key]


def extract_swagger_paths(content: str, filename: str) -> list[str]:
    if filename.endswith(".json"):
        try:
            data = json.loads(content)
            paths = data.get("paths", {}) if isinstance(data, dict) else {}
            if isinstance(paths, dict):
                return unique_paths([path for path in paths if isinstance(path, str)])
        except json.JSONDecodeError:
            pass

    return extract_yaml_paths(content)


def extract_yaml_paths(content: str) -> list[str]:
    paths: list[str] = []
    in_paths = False
    paths_indent = 0
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if re.match(r"^paths\s*:\s*$", stripped):
            in_paths = True
            paths_indent = indent
            continue
        if in_paths and indent <= paths_indent and not stripped.startswith("/"):
            break
        if in_paths:
            match = re.match(r"^['\"]?(/[^:'\"]+)['\"]?\s*:", stripped)
            if match:
                paths.append(match.group(1))
    return unique_paths(paths)


def calculate_confidence(
    basepath: str,
    card_url: str,
    proxy_infos: list[ProxyInfo],
    org: str,
    apigee_token: str,
    bundle_cache_dir: str,
    swagger_cache_dir: str,
    env_file: str,
    swagger_cache: dict[str, list[str]],
    flow_cache: dict[tuple[str, str], list[str]],
) -> str:
    if not proxy_infos:
        return ""

    swagger_paths = get_jira_swagger_paths(card_url, env_file, swagger_cache, swagger_cache_dir)
    if not swagger_paths:
        return ""

    apigee_paths: list[str] = []
    for info in proxy_infos:
        key = (info.proxy, info.revision)
        if key not in flow_cache:
            flow_cache[key] = get_revision_bundle_flow_paths(
                org,
                apigee_token,
                info.proxy,
                info.revision,
                bundle_cache_dir,
            )
        apigee_paths.extend(flow_cache[key])

    apigee_compare = {compare_path(path) for path in apigee_paths if compare_path(path)}
    if not apigee_compare:
        return ""

    comparable_swagger = [remove_basepath_prefix(path, basepath) for path in swagger_paths]
    total = len(comparable_swagger)
    matched = sum(1 for path in comparable_swagger if compare_path(path) in apigee_compare)
    return f"{round((matched / total) * 100)}%" if total else ""


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
    ensure_parent_dir(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=["base path", "proxy"])
        writer.writeheader()
        for basepath in sorted(mapping):
            writer.writerow({"base path": basepath, "proxy": ", ".join(mapping[basepath])})
    print(f"Wrote proxy map to {path}")


def add_proxy_to_env_files(
    input_prefix: str,
    output_prefix: str,
    output_suffix: str,
    proxy_map: dict[str, list[str]],
    proxy_info_map: dict[str, list[ProxyInfo]] | None = None,
    org: str = DEFAULT_ORG,
    apigee_token: str = "",
    bundle_cache_dir: str = DEFAULT_BUNDLE_CACHE_DIR,
    swagger_cache_dir: str = DEFAULT_SWAGGER_CACHE_DIR,
    env_file: str = ".env",
) -> None:
    with_confidence = proxy_info_map is not None
    swagger_cache: dict[str, list[str]] = {}
    flow_cache: dict[tuple[str, str], list[str]] = {}

    for env in ENVS:
        input_path = f"{input_prefix}_{env}.csv"
        output_path = f"{output_prefix}_{env}{output_suffix}.csv"
        input_rows: list[dict[str, str]] = []
        cards_with_proxy: set[str] = set()
        with open(input_path, newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            fieldnames = list(reader.fieldnames or [])
            for row in reader:
                basepath = normalize_basepath(row.get("base path", ""))
                row["proxy"] = ", ".join(proxy_map.get(basepath, []))
                if row["proxy"].strip():
                    cards_with_proxy.add(row.get("card", "").strip())
                input_rows.append(row)

        count = 0
        ensure_parent_dir(output_path)
        with open(input_path, newline="", encoding="utf-8-sig") as source, open(
            output_path, "w", newline="", encoding="utf-8-sig"
        ) as output:
            reader = csv.DictReader(source)
            fieldnames = list(reader.fieldnames or [])
            if "proxy" not in fieldnames:
                fieldnames.append("proxy")
            if with_confidence and "confident" not in fieldnames:
                fieldnames.append("confident")
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            for row in input_rows:
                if not row.get("proxy", "").strip() and row.get("card", "").strip() in cards_with_proxy:
                    continue
                count += 1
                if with_confidence and count % 50 == 0:
                    print(f"{env}: processed {count} row(s)", file=sys.stderr)
                basepath = normalize_basepath(row.get("base path", ""))
                if with_confidence:
                    row["confident"] = calculate_confidence(
                        basepath,
                        row.get("card", ""),
                        proxy_info_map.get(basepath, []) if proxy_info_map else [],
                        org,
                        apigee_token,
                        bundle_cache_dir,
                        swagger_cache_dir,
                        env_file,
                        swagger_cache,
                        flow_cache,
                    )
                writer.writerow(row)
        print(f"Wrote {count} row(s) to {output_path}")


def main() -> int:
    args = parse_args()
    proxy_info_map = None
    apigee_token = ""
    if args.use_cache:
        proxy_map = read_proxy_map(args.cache)
    elif args.source == "db":
        proxy_map = build_proxy_map_from_db(args.env_file)
        write_proxy_map(args.cache, proxy_map)
    else:
        proxy_info_map = build_proxy_info_from_db(args.env_file)
        proxy_map = {basepath: [item.proxy for item in infos] for basepath, infos in proxy_info_map.items()}
        try:
            apigee_token = get_access_token(args.token)
        except RuntimeError as error:
            print(f"warning: {error}", file=sys.stderr)
            print("warning: continuing with existing Apigee bundle cache only", file=sys.stderr)
            apigee_token = ""
        write_proxy_map(args.cache, proxy_map)

    add_proxy_to_env_files(
        args.input_prefix,
        args.output_prefix,
        args.output_suffix,
        proxy_map,
        proxy_info_map=proxy_info_map,
        org=args.org,
        apigee_token=apigee_token,
        bundle_cache_dir=args.bundle_cache_dir,
        swagger_cache_dir=args.swagger_cache_dir,
        env_file=args.env_file,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
