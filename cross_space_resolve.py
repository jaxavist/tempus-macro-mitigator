#!/usr/bin/env python3
"""
Cross-Space Include-Shared-Block Resolution
=============================================
Finds remaining include-shared-block macros that reference pages in OTHER spaces
and replaces them with excerpt-include macros pointing to the child pages
created during each space's migration.

For example:
  include-shared-block source="GLOS:Payment Card Industry Data Security Standard" key="General Definition"
  → excerpt-include pointing to "GLOS:_Payment Card Industry Data Security Standard - General Definition"

Usage:
    # Scan a space for remaining cross-space includes (dry run)
    python cross_space_resolve.py --space-key POL --dry-run

    # Live run
    python cross_space_resolve.py --space-key POL --batch-size 20

Environment variables:
    CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN
"""

import argparse
import copy
import json
import logging
import os
import sys
import time
import uuid

import requests

BASE_URL = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("CONFLUENCE_EMAIL", "")
API_TOKEN = os.environ.get("CONFLUENCE_API_TOKEN", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("cross_space")

session = requests.Session()
session.auth = (EMAIL, API_TOKEN)
session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})

# Cache: space_key → {page_title → page_id}
space_page_cache = {}


def get_space_id(space_key):
    resp = session.get(f"{BASE_URL}/wiki/api/v2/spaces", params={"keys": space_key})
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0]["id"] if results else None


def get_all_pages(space_id):
    pages = []
    cursor = None
    while True:
        params = {"limit": 250, "status": "current", "sort": "id"}
        if cursor:
            params["cursor"] = cursor
        resp = session.get(f"{BASE_URL}/wiki/api/v2/spaces/{space_id}/pages", params=params)
        resp.raise_for_status()
        data = resp.json()
        pages.extend((p["id"], p["title"]) for p in data.get("results", []))
        nl = data.get("_links", {}).get("next")
        if not nl or "cursor=" not in nl:
            break
        cursor = nl.split("cursor=")[1].split("&")[0]
    return pages


def get_page_body(page_id):
    resp = session.get(f"{BASE_URL}/wiki/api/v2/pages/{page_id}", params={"body-format": "atlas_doc_format"})
    resp.raise_for_status()
    d = resp.json()
    return {"id": d["id"], "title": d["title"], "version": d["version"]["number"],
            "body": json.loads(d["body"]["atlas_doc_format"]["value"])}


def update_page(page_id, title, version, body):
    msg = "Cross-space: include-shared-block → excerpt-include"
    resp = session.put(f"{BASE_URL}/wiki/api/v2/pages/{page_id}", json={
        "id": page_id, "status": "current", "title": title,
        "body": {"representation": "atlas_doc_format", "value": json.dumps(body)},
        "version": {"number": version + 1, "message": msg},
    })
    if resp.ok:
        return True
    if resp.status_code in (404, 500):
        resp2 = session.put(f"{BASE_URL}/wiki/rest/api/content/{page_id}", json={
            "type": "page", "title": title,
            "version": {"number": version + 1, "message": msg},
            "body": {"atlas_doc_format": {"value": json.dumps(body), "representation": "atlas_doc_format"}},
        })
        if resp2.ok:
            return True
    return False


def find_child_page_in_space(space_key, child_title):
    """Check if a child page exists in another space. Uses cache."""
    if space_key not in space_page_cache:
        log.info(f"  Indexing space '{space_key}'...")
        sid = get_space_id(space_key)
        if not sid:
            space_page_cache[space_key] = {}
            return None
        pages = get_all_pages(sid)
        space_page_cache[space_key] = {t: pid for pid, t in pages}
        log.info(f"  Indexed {len(space_page_cache[space_key])} pages in {space_key}")

    return space_page_cache[space_key].get(child_title)


def get_macro_param(node, param_name):
    params = node.get("attrs", {}).get("parameters", {}).get("macroParams", {})
    val = params.get(param_name, {})
    return val.get("value", "") if isinstance(val, dict) else val


def make_excerpt_include(child_page_title, space_key):
    return {
        "type": "extension",
        "attrs": {
            "extensionType": "com.atlassian.confluence.macro.core",
            "extensionKey": "excerpt-include",
            "parameters": {
                "macroParams": {
                    "": {"value": f"{space_key}:{child_page_title}"},
                    "nopanel": {"value": "true"},
                },
                "macroMetadata": {"macroId": {"value": str(uuid.uuid4())},
                                 "schemaVersion": {"value": "1"}, "title": "Excerpt Include"},
            },
        },
    }


def transform_node(node, stats, current_space):
    ext_key = node.get("attrs", {}).get("extensionKey", "")
    ext_type = node.get("attrs", {}).get("extensionType", "")

    if ext_key in ("include-shared-block", "include-shared-block-inline"):
        source_page = get_macro_param(node, "page")
        sb_key = get_macro_param(node, "shared-block-key")

        if not source_page or not sb_key:
            return [node]

        # Parse space:page format
        if ":" in source_page:
            target_space, page_title = source_page.split(":", 1)
        else:
            # Same-space reference — skip (already handled by consolidated script)
            return [node]

        # Skip if target space is same as current
        if target_space == current_space:
            return [node]

        # Look for the child page in the target space
        child_title = f"_{page_title} - {sb_key}"
        child_id = find_child_page_in_space(target_space, child_title)

        if child_id:
            stats["resolved"] += 1
            log.debug(f"    Resolved: {source_page}/{sb_key} → {target_space}:{child_title}")
            return [make_excerpt_include(child_title, target_space)]

        # Also try without the key (some pages have empty keys)
        if not sb_key:
            stats["unresolved"] += 1
            return [node]

        # Try the page title directly as a child page (maybe no key suffix)
        child_id_alt = find_child_page_in_space(target_space, f"_{page_title}")
        if child_id_alt:
            stats["resolved"] += 1
            return [make_excerpt_include(f"_{page_title}", target_space)]

        stats["unresolved"] += 1
        log.debug(f"    Unresolved: {source_page}/{sb_key} — child '{child_title}' not found in {target_space}")
        return [node]

    # Also handle inside legacy-content nestedContent
    if ext_type == "com.atlassian.confluence.migration" and ext_key == "legacy-content":
        nested = node.get("attrs", {}).get("parameters", {}).get("nestedContent", {})
        if nested and nested.get("content"):
            new_content = []
            for child in nested["content"]:
                new_content.extend(transform_node(child, stats, current_space))
            nested["content"] = new_content
        return [node]

    # Recurse
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(transform_node(child, stats, current_space))
        node["content"] = new_content

    return [node]


def has_cross_space_includes(node, current_space):
    """Quick check if a node tree has any cross-space include-shared-blocks."""
    ext_key = node.get("attrs", {}).get("extensionKey", "")
    if ext_key in ("include-shared-block", "include-shared-block-inline"):
        source_page = get_macro_param(node, "page")
        if source_page and ":" in source_page:
            target_space = source_page.split(":", 1)[0]
            if target_space != current_space:
                return True

    for child in node.get("content", []):
        if has_cross_space_includes(child, current_space):
            return True

    # Check inside legacy nestedContent
    ext_type = node.get("attrs", {}).get("extensionType", "")
    if ext_type == "com.atlassian.confluence.migration" and ext_key == "legacy-content":
        nested = node.get("attrs", {}).get("parameters", {}).get("nestedContent", {})
        if nested:
            for child in nested.get("content", []):
                if has_cross_space_includes(child, current_space):
                    return True

    return False


def main():
    parser = argparse.ArgumentParser(description="Cross-space include-shared-block resolution")
    parser.add_argument("--space-key", required=True, help="Space to scan for cross-space includes")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--batch-delay", type=float, default=1.0)
    parser.add_argument("--output-json")
    args = parser.parse_args()

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    current_space = args.space_key
    space_id = get_space_id(current_space)
    if not space_id:
        print(f"Space '{current_space}' not found")
        sys.exit(1)

    log.info(f"Scanning space '{current_space}' for cross-space include-shared-blocks...")
    all_pages = get_all_pages(space_id)
    pages = [(pid, t) for pid, t in all_pages if not t.startswith("_")]
    log.info(f"Pages to scan: {len(pages)}")

    totals = {"scanned": 0, "modified": 0, "skipped": 0, "errored": 0,
              "resolved": 0, "unresolved": 0}
    results = []

    for batch_start in range(0, len(pages), args.batch_size):
        batch = pages[batch_start:batch_start + args.batch_size]
        batch_num = (batch_start // args.batch_size) + 1
        total_batches = (len(pages) + args.batch_size - 1) // args.batch_size
        log.info(f"--- Batch {batch_num}/{total_batches} ---")

        for page_id, title in batch:
            totals["scanned"] += 1
            try:
                page = get_page_body(page_id)

                # Quick check — skip pages without cross-space includes
                if not has_cross_space_includes(page["body"], current_space):
                    totals["skipped"] += 1
                    continue

                body = copy.deepcopy(page["body"])
                stats = {"resolved": 0, "unresolved": 0}

                if "content" in body:
                    new_content = []
                    for child in body["content"]:
                        new_content.extend(transform_node(child, stats, current_space))
                    body["content"] = new_content

                if stats["resolved"] > 0:
                    if args.dry_run:
                        totals["modified"] += 1
                        log.info(f"  [{title}] Would resolve {stats['resolved']} cross-space includes")
                    else:
                        ok = update_page(page_id, title, page["version"], body)
                        if ok:
                            totals["modified"] += 1
                        else:
                            totals["errored"] += 1
                    totals["resolved"] += stats["resolved"]
                    totals["unresolved"] += stats["unresolved"]
                    results.append({"page_id": page_id, "title": title,
                                    "resolved": stats["resolved"], "unresolved": stats["unresolved"]})
                else:
                    totals["skipped"] += 1
                    totals["unresolved"] += stats["unresolved"]

            except Exception as e:
                totals["errored"] += 1
                log.error(f"  [{title}] Error: {e}")

        if batch_start + args.batch_size < len(pages) and args.batch_delay > 0:
            time.sleep(args.batch_delay)

    mode = "DRY RUN" if args.dry_run else "LIVE RUN"
    print(f"\n{'=' * 60}")
    print(f"  CROSS-SPACE RESOLUTION ({mode}) — {current_space}")
    print(f"{'=' * 60}")
    print(f"  Pages scanned:          {totals['scanned']}")
    print(f"  Pages with resolutions: {totals['modified']}")
    print(f"  Pages skipped:          {totals['skipped']}")
    print(f"  Pages with errors:      {totals['errored']}")
    print(f"  ---")
    print(f"  Includes resolved:      {totals['resolved']}")
    print(f"  Includes unresolved:    {totals['unresolved']}")
    print(f"{'=' * 60}")

    if totals["unresolved"] > 0:
        print(f"\n  Note: Unresolved includes reference pages where no matching")
        print(f"  child page (_{'{'}PageTitle{'}'} - {'{'}Key{'}'}) was found in the target space.")
        print(f"  These may be references to archived/deleted pages or pages")
        print(f"  that weren't processed during migration.")

    if args.output_json:
        with open(os.path.expanduser(args.output_json), "w") as f:
            json.dump({"summary": totals, "pages": results}, f, indent=2)


if __name__ == "__main__":
    main()
