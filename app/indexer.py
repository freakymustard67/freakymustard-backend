"""MongoDB indexer for the Tamil catalogue — duplicate-proof by design.

History lesson (2026-08): this indexer used to upsert by ABSOLUTE url while
the source site cycled through 11 mirror domains, which duplicated the whole
catalogue on every migration (30,958 docs for 3,480 movies). Documents are
now keyed by ``path_key`` — the url path, domain-independent — under a
UNIQUE index, and stored links are always rewritten to the domain the
scraper currently resolves to. A mirror move can no longer create duplicates;
it just updates the ``link`` field of the existing doc.
"""

import asyncio
import os

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

from scraper import MoviesdaScraper, path_key

MONGO_URI = os.environ.get("MONGO_URI")
DB_NAME = "moviesda_db"
COLLECTION_NAME = "movies"


class MovieIndexer:
    def __init__(self):
        self.scraper = MoviesdaScraper()
        self.client = None
        self.collection = None
        self.is_indexing = False

        if MONGO_URI:
            try:
                self.client = AsyncIOMotorClient(MONGO_URI)
                self.collection = self.client[DB_NAME][COLLECTION_NAME]
                print("Indexer: Connected to MongoDB.")
            except Exception as e:
                print(f"Indexer: Failed to connect to MongoDB: {e}")
        else:
            print("Indexer: WARNING - MONGO_URI not found. Indexing will not be persisted.")

    async def ensure_indexes(self) -> None:
        """Idempotent; the unique index is the duplicate-prevention guarantee."""
        if self.collection is None:
            return
        try:
            await self.collection.create_index("path_key", unique=True)
        except DuplicateKeyError as e:
            # Pre-existing duplicates block the build — should be impossible
            # after the 2026-08 cleanup, but never crash startup over it.
            print(f"Indexer: unique index build blocked by legacy duplicates: {e}")

    async def _upsert(self, movie: dict) -> None:
        """Insert/update by path_key, merging metadata instead of overwriting."""
        key = path_key(movie.get("link"))
        if not key:
            return  # nav/junk link with no path — never indexable
        movie["path_key"] = key

        # Stored link always points at the domain the scraper is reaching
        # right now, so lookups survive mirror migrations.
        base = self.scraper.resolved_base
        if base:
            movie["link"] = f"{base}{key}"

        existing = await self.collection.find_one({"path_key": key})
        if existing:
            # Never clobber good metadata with nulls from a re-scan.
            updates = {k: v for k, v in movie.items() if v is not None or existing.get(k) is None}
            await self.collection.update_one({"path_key": key}, {"$set": updates})
        else:
            try:
                await self.collection.insert_one(movie)
            except DuplicateKeyError:
                await self.collection.update_one(
                    {"path_key": key}, {"$set": {k: v for k, v in movie.items() if v is not None}}
                )

    async def start_indexing(self):
        """Main indexing loop."""
        if self.is_indexing:
            print("Indexer: Already running.")
            return

        if self.collection is None:
            print("Indexer: No MongoDB connection. Skipping indexing.")
            return

        self.is_indexing = True
        print("Indexer: Starting background indexing...")

        try:
            await self.ensure_indexes()
            years = await self.scraper.get_years()
            print(f"Indexer: Found {len(years)} year categories.")

            for year in years:
                print(f"Indexer: Scanning {year['name']}...")
                base_url = year["link"]

                page = 1
                last_page_links = set()
                while True:
                    if page == 1:
                        url = base_url
                    else:
                        separator = "?" if base_url.endswith("/") else "/?"
                        url = f"{base_url}{separator}page={page}"

                    try:
                        movies = await self.scraper.get_movies_in_year(url)
                    except Exception as e:
                        print(f"Indexer: Error scanning {url}: {e}")
                        break

                    if not movies:
                        break

                    # Detect pagination loops (site serving the same page /
                    # redirecting to home)
                    current_links = set(m["link"] for m in movies)
                    if current_links == last_page_links:
                        print(f"Indexer: Page {page} identical to previous. Stopping {year['name']}.")
                        break
                    last_page_links = current_links

                    for m in movies:
                        m["year_category"] = year["name"]
                        try:
                            existing = await self.collection.find_one(
                                {"path_key": path_key(m["link"])}
                            )
                            if existing and existing.get("poster"):
                                pass  # already deep-indexed
                            else:
                                details = await self.scraper.get_qualities(m["link"])
                                meta = details.get("meta", {})
                                if meta.get("poster"):
                                    m["poster"] = meta["poster"]
                                if meta.get("desc"):
                                    m["desc"] = meta["desc"]
                                print(f"   > Deep indexed: {m['title']}")
                        except Exception as e:
                            print(f"   > Failed deep index for {m['title']}: {e}")

                        await self._upsert(m)

                    await asyncio.sleep(0.2)  # polite delay
                    page += 1

            print("Indexer: Indexing complete.")

        except Exception as e:
            print(f"Indexer: Critical failure: {e}")
        finally:
            self.is_indexing = False

    async def search(self, query: str):
        """Search via MongoDB regex."""
        if not query or self.collection is None:
            return []

        cursor = self.collection.find(
            {"title": {"$regex": query, "$options": "i"}}
        ).limit(50)

        results = await cursor.to_list(length=50)
        for doc in results:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
        return results

    async def enrich_metadata(self, movies):
        """Enrich live-scraped movies with cached metadata (matched by path)."""
        if self.collection is None:
            return movies

        keys = [path_key(m.get("link")) for m in movies]
        keys = [k for k in keys if k]
        if not keys:
            return movies

        cursor = self.collection.find({"path_key": {"$in": keys}})
        cached_docs = await cursor.to_list(length=len(keys))
        cached_map = {d.get("path_key"): d for d in cached_docs}

        for movie in movies:
            cached = cached_map.get(path_key(movie.get("link")))
            if cached:
                # Prefer the cached LIVE link — it is kept current by the
                # indexer even when the mirror domain moves.
                if cached.get("link"):
                    movie["link"] = cached["link"]
                if not movie.get("poster") and cached.get("poster"):
                    movie["poster"] = cached["poster"]
                if not movie.get("desc") and cached.get("desc"):
                    movie["desc"] = cached["desc"]

        return movies
