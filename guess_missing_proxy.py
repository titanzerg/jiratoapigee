#!/usr/bin/env python3
"""Guess Apigee proxy for rows that did not map by base path."""

from __future__ import annotations

import argparse
import csv
import glob
import io
import os
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime

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
    get_revision_bundle_flow_paths,
    read_or_download_revision_bundle,
    normalize_basepath,
    remove_basepath_prefix,
)


DEFAULT_INPUT_GLOB = "data/add_apigee_proxy_name/jira_latest_by_basepath_*_with_proxy.csv"
DEFAULT_OUTPUT = "data/guess_missing_proxy/jira_missing_proxy_guess.csv"
DEFAULT_JIRA_EXPORT = "data/jira_export/jira_api_support_export.csv"


@dataclass
class Guess:
    proxy: str = ""
    confidence: int = 0
    desc_basepath: str = ""


@dataclass
class ProxyScore:
    proxy: str
    confidence: int
    desc_basepath: str
    basepath_score: tuple[int, int, int, int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guess proxy names for unmapped Jira cards.")
    parser.add_argument("--input-glob", default=DEFAULT_INPUT_GLOB)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--jira-export", default=DEFAULT_JIRA_EXPORT)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--org", default=DEFAULT_ORG)
    parser.add_argument("--token", help="OAuth access token. Defaults to `gcloud auth print-access-token`.")
    parser.add_argument("--bundle-cache-dir", default=DEFAULT_BUNDLE_CACHE_DIR)
    parser.add_argument("--swagger-cache-dir", default=DEFAULT_SWAGGER_CACHE_DIR)
    parser.add_argument("--min-confidence", type=int, default=50)
    return parser.parse_args()


def parse_created_date(value: str) -> datetime:
    cleaned = value.strip()
    if not cleaned:
        return datetime.min
    for candidate in (cleaned, cleaned.replace("Z", "+0000")):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(candidate, fmt)
                return parsed.replace(tzinfo=None)
            except ValueError:
                pass
    return datetime.min


def read_created_dates(path: str) -> dict[str, datetime]:
    created_by_card: dict[str, datetime] = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            for row in reader:
                card = row.get("link", "").strip()
                if card:
                    created_by_card[card] = parse_created_date(row.get("create date", ""))
    except FileNotFoundError:
        print(f"warning: {path} not found; sorting ties by card only", file=sys.stderr)
    return created_by_card


def read_missing_rows(input_glob: str) -> dict[str, set[str]]:
    by_card: dict[str, set[str]] = {}
    for path in sorted(glob.glob(input_glob)):
        with open(path, newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            for row in reader:
                if row.get("proxy", "").strip():
                    continue
                card = row.get("card", "").strip()
                basepath = normalize_basepath(row.get("base path", ""))
                if card:
                    by_card.setdefault(card, set()).add(basepath)
    return by_card


def flatten_proxy_infos(proxy_info_by_basepath: dict[str, list[ProxyInfo]]) -> list[ProxyInfo]:
    seen: set[tuple[str, str]] = set()
    result: list[ProxyInfo] = []
    for infos in proxy_info_by_basepath.values():
        for info in infos:
            key = (info.proxy, info.revision)
            if key not in seen:
                seen.add(key)
                result.append(info)
    return sorted(result, key=lambda item: item.proxy.lower())


def build_proxy_basepaths(proxy_info_by_basepath: dict[str, list[ProxyInfo]]) -> dict[str, list[str]]:
    by_proxy: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for basepath, infos in proxy_info_by_basepath.items():
        for info in infos:
            key = (info.proxy, basepath)
            if key in seen:
                continue
            seen.add(key)
            by_proxy.setdefault(info.proxy, []).append(basepath)
    return {proxy: sorted(paths) for proxy, paths in by_proxy.items()}


def build_flow_index(
    proxy_infos: list[ProxyInfo],
    org: str,
    token: str,
    bundle_cache_dir: str,
) -> dict[ProxyInfo, set[str]]:
    index: dict[ProxyInfo, set[str]] = {}
    total = len(proxy_infos)
    for idx, info in enumerate(proxy_infos, start=1):
        if idx % 25 == 0:
            print(f"loaded flow paths for {idx}/{total} proxy revision(s)", file=sys.stderr)
        paths = get_revision_bundle_flow_paths(org, token, info.proxy, info.revision, bundle_cache_dir)
        comparable = {compare_path(path) for path in paths if compare_path(path)}
        if comparable:
            index[info] = comparable
    return index


def build_basepath_flow_index(
    proxy_infos: list[ProxyInfo],
    org: str,
    token: str,
    bundle_cache_dir: str,
) -> dict[ProxyInfo, dict[str, set[str]]]:
    index: dict[ProxyInfo, dict[str, set[str]]] = {}
    for info in proxy_infos:
        bundle = read_or_download_revision_bundle(org, token, info.proxy, info.revision, bundle_cache_dir)
        if not bundle:
            continue
        basepath_paths: dict[str, set[str]] = {}
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            for name in archive.namelist():
                if not name.endswith(".xml") or "/proxies/" not in name:
                    continue
                content = archive.read(name).decode("utf-8", errors="replace")
                basepaths = extract_xml_basepaths(content)
                if not basepaths:
                    continue
                flow_paths = {compare_path(path) for path in extract_condition_paths(content) if compare_path(path)}
                if not flow_paths:
                    continue
                for basepath in basepaths:
                    basepath_paths.setdefault(normalize_basepath(basepath), set()).update(flow_paths)
        if basepath_paths:
            index[info] = basepath_paths
    return index


def score_proxy(swagger_paths: list[str], basepaths: set[str], apigee_paths: set[str]) -> int:
    comparable_swagger: set[str] = set()
    for path in swagger_paths:
        candidates = [path]
        for basepath in basepaths:
            if basepath:
                candidates.append(remove_basepath_prefix(path, basepath))
        for candidate in candidates:
            comparable = compare_path(candidate)
            if comparable:
                comparable_swagger.add(comparable)

    if not comparable_swagger or not apigee_paths:
        return 0
    matched = comparable_swagger & apigee_paths
    return round((len(matched) / len(comparable_swagger)) * 100)


def guess_for_card(
    card: str,
    basepaths: set[str],
    flow_index: dict[ProxyInfo, set[str]],
    basepath_flow_index: dict[ProxyInfo, dict[str, set[str]]],
    proxy_basepaths: dict[str, list[str]],
    env_file: str,
    swagger_cache_dir: str,
    swagger_cache: dict[str, list[str]],
) -> Guess:
    swagger_paths = get_jira_swagger_paths(card, env_file, swagger_cache, swagger_cache_dir)
    if not swagger_paths:
        return Guess()

    best = Guess()
    tied: list[ProxyScore] = []
    for info, apigee_paths in flow_index.items():
        confidence = score_proxy(swagger_paths, basepaths, apigee_paths)
        desc_basepath, basepath_score = best_desc_basepath_for_proxy(
            swagger_paths,
            proxy_basepaths.get(info.proxy, []),
            apigee_paths,
            basepath_flow_index.get(info, {}),
        )
        if confidence > best.confidence:
            best = Guess(
                proxy=info.proxy,
                confidence=confidence,
                desc_basepath=desc_basepath,
            )
            tied = [ProxyScore(info.proxy, confidence, desc_basepath, basepath_score)]
        elif confidence == best.confidence and confidence > 0:
            tied.append(ProxyScore(info.proxy, confidence, desc_basepath, basepath_score))

    if len(tied) > 1:
        best_basepath_score = max(item.basepath_score for item in tied)
        best_tied = [item for item in tied if item.basepath_score == best_basepath_score]
        best.proxy = ", ".join(sorted({item.proxy for item in best_tied}))
        best.desc_basepath = sorted(item.desc_basepath for item in best_tied if item.desc_basepath)[0]
    return best


def best_desc_basepath_for_proxy(
    swagger_paths: list[str],
    proxy_basepaths: list[str],
    apigee_paths: set[str],
    basepath_flow_paths: dict[str, set[str]],
) -> tuple[str, tuple[int, int, int, int]]:
    scored: list[tuple[tuple[int, int, int, int], str]] = []
    for basepath in proxy_basepaths:
        normalized = normalize_basepath(basepath)
        if basepath_flow_paths:
            paths_for_basepath = basepath_flow_paths.get(normalized, set())
        else:
            paths_for_basepath = apigee_paths
        score = score_basepath(swagger_paths, normalized, paths_for_basepath)
        scored.append((score, normalized))
    if not scored:
        return "", (0, 0, 0, 0)

    best_score, best_basepath = max(scored, key=lambda item: (item[0], -len(item[1]), item[1]))
    return format_desc_basepath([best_basepath]), best_score


def score_basepath(swagger_paths: list[str], basepath: str, apigee_paths: set[str]) -> tuple[int, int, int, int]:
    normalized = normalize_basepath(basepath)
    comparable = {
        compare_path(remove_basepath_prefix(path, normalized))
        for path in swagger_paths
        if compare_path(remove_basepath_prefix(path, normalized))
    }
    matched_paths = len(comparable & apigee_paths)
    confidence = round((matched_paths / len(comparable)) * 100) if comparable and apigee_paths else 0
    prefix_hits = sum(1 for path in swagger_paths if compare_path(path).startswith(compare_path(normalized + "/")))
    exact_hits = sum(1 for path in swagger_paths if compare_path(path) == compare_path(normalized))
    return (confidence, matched_paths, prefix_hits, exact_hits)


def format_desc_basepath(basepaths: list[str]) -> str:
    unique = []
    seen: set[str] = set()
    for basepath in basepaths:
        normalized = normalize_basepath(basepath)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    if not unique:
        return ""
    return "basepath: " + ", ".join(unique)




def write_output(path: str, rows: list[dict[str, str]]) -> None:
    ensure_parent_dir(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=["card", "guess_proxy", "confident", "desc_basepath"])
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in ["card", "guess_proxy", "confident", "desc_basepath"]})


def ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def sort_rows(rows: list[dict[str, str]], created_by_card: dict[str, datetime]) -> list[dict[str, str]]:
    def confidence_value(row: dict[str, str]) -> int:
        value = row.get("confident", "").strip().rstrip("%")
        return int(value) if value.isdigit() else -1

    return sorted(
        rows,
        key=lambda row: (
            confidence_value(row),
            created_by_card.get(row.get("card", ""), datetime.min),
            row.get("card", ""),
        ),
        reverse=True,
    )


def main() -> int:
    args = parse_args()
    missing = read_missing_rows(args.input_glob)
    created_by_card = read_created_dates(args.jira_export)
    print(f"Found {len(missing)} unmapped card(s)")

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

    rows: list[dict[str, str]] = []
    swagger_cache: dict[str, list[str]] = {}
    total = len(missing)
    for idx, (card, basepaths) in enumerate(sorted(missing.items()), start=1):
        if idx % 25 == 0:
            print(f"guessed {idx}/{total} card(s)", file=sys.stderr)
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
        if guess.confidence < args.min_confidence:
            rows.append({"card": card, "guess_proxy": "", "confident": "", "desc_basepath": ""})
        else:
            rows.append(
                {
                    "card": card,
                    "guess_proxy": guess.proxy,
                    "confident": f"{guess.confidence}%",
                    "desc_basepath": guess.desc_basepath,
                }
            )

    rows = sort_rows(rows, created_by_card)
    write_output(args.output, rows)
    print(f"Wrote {len(rows)} row(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
