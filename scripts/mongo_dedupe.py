"""One-shot MongoDB cleanup: backup, dedupe by URL path, unique index.

Duplication root cause: the indexer upserted by ABSOLUTE url while the
source site cycled through 11 mirror domains, so every migration inserted
a full copy of the catalog (~27.5k of ~31k docs were duplicates).

This script:
  1. Dumps the whole collection to a gzipped JSONL backup.
  2. Groups docs by URL path (domain-independent key). For each group it
     keeps one doc — preferring one from the live domain — merges in any
     missing poster/desc/year_category from its siblings, rewrites `link`
     to the live domain, stamps `path_key`, and deletes the rest.
  3. Creates a unique index on `path_key` so duplicates are impossible
     even if the domain changes again.

Run: python mongo_dedupe.py <MONGO_URI> <LIVE_DOMAIN>
"""

import asyncio
import gzip
import json
import sys
import urllib.parse
from collections import defaultdict

from motor.motor_asyncio import AsyncIOMotorClient

DB_NAME = "moviesda_db"
COLLECTION = "movies"


def path_key(link: str) -> str:
    p = urllib.parse.urlparse(link or "")
    key = (p.path or "").rstrip("/")
    if p.query:
        key += "?" + p.query
    return key


async def main(uri: str, live_domain: str) -> None:
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=20000)
    coll = client[DB_NAME][COLLECTION]

    # --- 1. Backup -----------------------------------------------------------
    backup_path = f"/home/abhishek/moviesda_backup_{'2026-08-16'}.jsonl.gz"
    n = 0
    with gzip.open(backup_path, "wt", encoding="utf-8") as fh:
        async for doc in coll.find({}):
            doc["_id"] = str(doc["_id"])
            fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
            n += 1
    print(f"backup: {n} docs -> {backup_path}")

    # --- 2. Group by path ----------------------------------------------------
    groups: dict[str, list[dict]] = defaultdict(list)
    async for doc in coll.find({}):
        groups[path_key(doc.get("link"))].append(doc)

    total_docs = sum(len(g) for g in groups.values())
    deletable = total_docs - len(groups)
    print(f"groups: {len(groups)} unique paths over {total_docs} docs ({deletable} to delete)")

    to_delete: list = []
    merged = 0
    for key, docs in groups.items():
        if not key:
            # Unparseable links: keep untouched (no stable identity).
            continue
        # Prefer a doc already on the live domain, then richest metadata.
        def rank(d: dict) -> tuple:
            on_live = live_domain in (d.get("link") or "")
            return (
                on_live,
                bool(d.get("poster")),
                bool(d.get("desc")),
                len([f for f in ("poster", "desc", "year_category") if d.get(f)]),
            )

        docs.sort(key=rank, reverse=True)
        keep = docs[0]
        for sibling in docs[1:]:
            for field in ("poster", "desc", "year_category"):
                if not keep.get(field) and sibling.get(field):
                    keep[field] = sibling[field]
        keep["path_key"] = key
        keep["link"] = f"https://{live_domain}{key if key.startswith('/') else '/' + key}"

        to_delete.extend(d["_id"] for d in docs[1:])
        await coll.replace_one({"_id": keep["_id"]}, keep)
        merged += 1

    # Batched delete
    for i in range(0, len(to_delete), 1000):
        await coll.delete_many({"_id": {"$in": to_delete[i : i + 1000]}})
    print(f"deleted: {len(to_delete)} | kept+merged: {merged}")

    # --- 3. Unique index -----------------------------------------------------
    existing = await coll.index_information()
    if "path_key_1" in existing:
        await coll.drop_index("path_key_1")
    await coll.create_index("path_key", unique=True, background=True)
    final = await coll.count_documents({})
    print(f"final count: {final} | indexes: {list((await coll.index_information()).keys())}")
    client.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
