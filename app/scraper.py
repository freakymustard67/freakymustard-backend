"""Moviesda scraper — Tamil catalogue traversal (Levels 1-7).

Entry point is the ``gotopage.top`` landing page. It used to REDIRECT to the
live mirror; it is now a static directory whose year links point at the
current domain (moviesdatamil.co as of 2026-08). Every request still updates
``resolved_base`` from the final URL, so absolute links from the landing page
and relative links deeper in the site both resolve correctly.

Levels:
  1 get_years         — year categories from the landing page
  2 get_movies_in_year — movie links on a year page (paginated by caller)
  3 get_qualities     — quality/folder links + poster/description metadata
  4 get_files         — file entries (drills through wrapper folders)
  5 get_servers       — download-server links on a file page
  6 resolve_final_link — follows server pages to the direct .mp4/.mkv
"""

import asyncio
import httpx
from bs4 import BeautifulSoup
from typing import Optional, List, Dict
import time
import urllib.parse
import re

try:
    from fake_useragent import UserAgent

    _UA = UserAgent().random
except Exception:
    # fake-useragent data fetch can fail in locked-down containers; the CDN
    # only cares that we look like a normal browser.
    _UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

_FALLBACK_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# --- series-path mirror resolution -------------------------------------------
#
# The series listing lives at a fixed PATH on whichever mirror currently
# serves. The old code hardcoded the whole url (moviesdatamil.co), which is
# now only a redirector — and one that is DNS-poisoned on many networks — so
# the fetch raised and /api/series 500'd the shelf away. The domain is now
# resolved at call time from the same live signal the movie path uses
# (resolved_base / the directory page), with these seeds as the last resort.
SERIES_PATH = "/tamil-web-series-download/"

# Seeds, newest first. A future mirror move costs one failed hop instead of
# an outage; the first domain that answers is remembered and reused.
SERIES_SEED_BASES = (
    "https://moviezda.net",
    "https://moviesdatamil.me",
    "https://moviezda.com",
    "https://moviesdatamil.co",
)

SERIES_FETCH_TIMEOUT = 20.0  # per candidate: a dead mirror must fail fast
MIRROR_DISCOVERY_TIMEOUT = 10.0
MIRROR_DISCOVERY_TTL = 600.0  # don't re-read the directory on every request


class SeriesListingUnavailable(RuntimeError):
    """No known mirror served the series listing — upstream trouble, not a bug."""


def path_key(link: str) -> str:
    """Domain-independent identity of a scraped url (see indexer)."""
    p = urllib.parse.urlparse(link or "")
    key = (p.path or "").rstrip("/")
    if p.query:
        key += "?" + p.query
    return key


def canonical_page_url(url: str) -> str:
    """Normalize a site page url to the form the site actually serves.

    Directory-style pages 302 to a junk ``movies.php`` unless the path ends
    with ``/`` — a slash-less url must never reach the scraper. External
    download-chain urls (with a query or a dotted last segment) pass through
    untouched.
    """
    if not url:
        return url
    p = urllib.parse.urlparse(url)
    if p.query or p.path.endswith("/"):
        return url
    last = p.path.rsplit("/", 1)[-1]
    if "." in last:  # e.g. movies.php — a real file
        return url
    return url.rstrip("/") + "/"


class MoviesdaScraper:
    def __init__(self):
        self.headers = dict(_FALLBACK_HEADERS)
        self.base_url = "https://gotopage.top/?ref=2026"  # Seed/directory URL
        self.client = httpx.AsyncClient(headers=self.headers, follow_redirects=True, timeout=30.0)
        self.resolved_base = None
        self.series_base = None  # last domain that actually served the series listing
        self.mirror_bases: List[str] = []  # live domains harvested from the directory
        self._mirror_checked_at = 0.0

    async def _get_soup(self, url: str, timeout: Optional[float] = None) -> BeautifulSoup:
        print(f"Fetching: {url}")
        # ``timeout=None`` here means "client default"; httpx treats an
        # explicit None as "no timeout", so only pass it when set.
        kwargs = {} if timeout is None else {"timeout": timeout}
        response = await self.client.get(url, **kwargs)
        response.raise_for_status()
        # Track the domain we actually landed on — the mirror moves often.
        final_url = str(response.url)
        parsed = urllib.parse.urlparse(final_url)
        self.resolved_base = f"{parsed.scheme}://{parsed.netloc}"
        return BeautifulSoup(response.text, "html.parser")

    async def _resolve_url(self, relative_url: str) -> str:
        if relative_url.startswith("http"):
            return relative_url
        if not self.resolved_base:
            await self._get_soup(self.base_url)
        return urllib.parse.urljoin(self.resolved_base, relative_url)

    async def get_years(self) -> List[Dict[str, str]]:
        """Level 1: Get list of years (links on the landing page)."""
        soup = await self._get_soup(self.base_url)
        items = []
        for a in soup.find_all("a"):
            text = a.get_text(strip=True)
            if re.search(r"\d{4}\s+Movies", text, re.IGNORECASE):
                items.append({"name": text, "link": await self._resolve_url(a["href"])})
        return items

    @staticmethod
    def _is_nav_junk(text: str) -> bool:
        """Navigation/pagination artifacts that historically polluted the index."""
        if re.fullmatch(r"\d+", text):
            return True
        if text.lower() in ("home", "moviesda home"):
            return True
        # Pagination arrows ("»", "«", "›"…) render as blank, link-less tiles.
        if text and not text.strip("«»‹›<>→←…").strip():
            return True
        return False

    async def get_movies_in_year(self, year_url: str) -> List[Dict[str, str]]:
        """Level 2: List movies in a specific year page."""
        soup = await self._get_soup(canonical_page_url(year_url))
        items = []
        for a_tag in soup.find_all("a"):
            text = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")

            # Filtering logic
            if not href: continue
            if "page" in href.lower(): continue
            if len(text) <= 1: continue # Skip A, B, C navigation
            if text == "0-9": continue
            if self._is_nav_junk(text): continue
            if "telegram" in text.lower(): continue
            if "group" in text.lower(): continue
            if "home" in text.lower(): continue
            if "dmca" in text.lower() or "disclaimer" in text.lower(): continue

            # Filter year-category backlinks ("Tamil 2025 Movies", "2025 Movies")
            if "movies" in text.lower() and re.search(r"\d{4}", text):
                if text.lower().startswith("tamil") and text.lower().endswith("movies"):
                     continue
                if text.lower().startswith("20") and text.lower().endswith("movies"):
                     continue

            items.append({"title": text, "link": await self._resolve_url(href)})

        # Deduplicate by domain-independent key (mirrors reuse the same slugs)
        seen = set()
        unique_items = []
        for i in items:
            k = path_key(i["link"])
            if k and k not in seen:
                seen.add(k)
                unique_items.append(i)
        return unique_items

    async def get_qualities(self, movie_url: str) -> Dict[str, any]:
        """Level 3: Get available qualities (e.g. Original, 640x360) AND Metadata."""
        soup = await self._get_soup(canonical_page_url(movie_url))

        # --- Metadata Extraction ---
        meta = {"poster": None, "desc": None, "rating": None}

        # 1. Poster — first substantial non-icon image
        try:
            for img in soup.find_all("img"):
                src = img.get("src")
                if not src: continue
                if "folder" in src or "arrow" in src or "dir" in src: continue
                meta["poster"] = await self._resolve_url(src)
                break
        except Exception:
            pass

        try:
            candidates = []
            info_node = soup.find(string=re.compile(r"Movie Information", re.I))
            if info_node:
                 candidates.append(info_node.parent.get_text(separator=" ", strip=True))
            synopsis_node = soup.find(string=re.compile(r"Synopsis\s*:", re.I))
            if synopsis_node:
                 candidates.append(synopsis_node.parent.get_text(separator=" ", strip=True))
            for element in soup.find_all(["p", "div", "font"]):
                text = element.get_text(separator=" ", strip=True)
                lower_text = text.lower()
                if "director:" in lower_text or "synopsis:" in lower_text:
                    candidates.append(text)

            best_desc = None
            for text in candidates:
                cleaned_text = re.sub(r"\s+", " ", text).strip()
                split_match = re.search(r"Movie\s+Information", cleaned_text, re.IGNORECASE)
                if split_match:
                    cleaned_text = cleaned_text[split_match.start():]

                stop_patterns = [
                    r"Incoming\s+Search\s+Terms",
                    r"Page\s+Tags",
                    r"Moviesda\s+Home",
                    r"Disclaimer",
                    r"A-Z\s+Movie\s+Categories",
                    r"Join\s+our\s+Telegram",
                ]
                for pattern in stop_patterns:
                    m_match = re.search(pattern, cleaned_text, re.IGNORECASE)
                    if m_match:
                        cleaned_text = cleaned_text[:m_match.start()].strip()

                cleaned_text = cleaned_text.strip()
                if len(cleaned_text) > 30 and ("Director" in cleaned_text or "Synopsis" in cleaned_text or "Movie:" in cleaned_text):
                     if "Movie Information" in cleaned_text:
                         best_desc = cleaned_text
                         break
                     if not best_desc:
                         best_desc = cleaned_text

            if best_desc:
                meta["desc"] = best_desc
        except Exception as e:
            print(f"Error extracting metadata: {e}")

        # --- Quality Links ---
        items = []
        blocks = soup.find_all("div", class_="f")
        if not blocks: blocks = soup.find_all("a")  # Fallback

        for block in blocks:
            a_tag = block if block.name == "a" else block.find("a")
            if not a_tag: continue
            text = a_tag.get_text(strip=True)
            href = a_tag.get("href")
            if not href or "page" in href: continue
            if self._is_nav_junk(text): continue
            if "telegram" in text.lower() or "whatsapp" in text.lower(): continue

            items.append({"name": text, "link": await self._resolve_url(href)})

        return {"qualities": items, "meta": meta}

    async def get_files(self, quality_url: str, depth: int = 0) -> List[Dict[str, str]]:
        """Level 4: Get actual file entries (Drills down if it finds folders)."""
        if depth > 2:
            return []

        result = await self.get_qualities(quality_url)
        items = result.get("qualities", [])

        # A single wrapper folder (Movie -> Original -> Tamil -> 720p) gets
        # flattened by peeking one level deeper.
        if len(items) == 1:
            item = items[0]
            print(f"Drill checking (Depth {depth}): Found single item '{item['name']}'. Peeking one level deeper...")
            sub_items = await self.get_files(item["link"], depth=depth + 1)
            if sub_items:
                print(f"Drill success: Flattening {len(sub_items)} items from sub-folder.")
                return sub_items
            return items

        return items

    async def get_servers(self, file_url: str) -> List[Dict[str, str]]:
        """Level 5: Get download servers."""
        soup = await self._get_soup(file_url)
        items = []
        for a in soup.find_all("a"):
            text = a.get_text(strip=True)
            href = a.get("href", "")
            if (("server" in text.lower() or "download" in text.lower() or "link" in text.lower()) or "download" in href.lower()) and "home" not in text.lower() and "back" not in text.lower():
                  items.append({"server": text, "link": await self._resolve_url(href)})
        return items

    async def resolve_final_link(self, server_url: str, depth: int = 0, _visited: Optional[set] = None) -> Optional[str]:
        """Level 6+: Recursively follow redirect/server pages to get final media link.

        Two terminal states:
          - a page carrying a direct .mp4/.mkv/.m3u8 link, or
          - a url that IS the media (e.g. uptomkv ``download.php?dl=…`` streams
            the signed, expiring file directly — fetching it as HTML would
            pull the whole movie into memory, so hops are streamed and
            content-sniffed before parsing).

        ``_visited`` prevents the self-referential "Download Server 2" loops
        some mirror pages contain.
        """
        if depth > 6:
            print("Max depth reached in resolving link.")
            return None
        if _visited is None:
            _visited = set()
        if server_url in _visited:
            return None
        _visited.add(server_url)

        try:
            print(f"Resolving (Depth {depth}): {server_url[:110]}")
            req = self.client.build_request("GET", server_url, headers=self.headers)
            resp = await self.client.send(req, stream=True)

            final_url = str(resp.url)
            parsed = urllib.parse.urlparse(final_url)
            self.resolved_base = f"{parsed.scheme}://{parsed.netloc}"

            # --- Is this hop the media itself? ---
            ct = (resp.headers.get("content-type") or "").lower()
            cl = resp.headers.get("content-length")
            looks_html = "html" in ct or "xml" in ct or "json" in ct
            huge = cl and cl.isdigit() and int(cl) > 5_000_000
            if not looks_html or huge:
                await resp.aclose()
                return final_url

            body = await resp.aread()
            if len(body) > 5_000_000:  # binary served as text/html
                return final_url
            await resp.aclose()
            soup = BeautifulSoup(body, "html.parser")

            # 1. Direct download button (.mp4/.mkv/.m3u8) — success
            for a in soup.find_all("a"):
                href = a.get("href", "")
                low = href.lower()
                if low.endswith(".mp4") or low.endswith(".mkv") or low.endswith(".m3u8"):
                    return await self._resolve_url(href)

            # 2. "Download Server" buttons — recurse
            for a in soup.find_all("a"):
                text = a.get_text(strip=True)
                href = a.get("href", "")
                if ("download server" in text.lower() or "server" in text.lower() or "watch online" in text.lower()) and "home" not in text.lower():
                    next_link = await self._resolve_url(href)
                    if next_link != server_url:
                        result = await self.resolve_final_link(next_link, depth + 1, _visited)
                        if result:
                            return result

            # 3. Meta refresh fallback
            meta_refresh = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
            if meta_refresh:
                content = meta_refresh.get("content", "")
                if "url=" in content.lower():
                    next_url = content.split("url=")[-1].strip()
                    return await self._resolve_url(next_url)

            return None
        except Exception as e:
            print(f"Error resolving final link: {e}")
            return None

    # --- Tamil web series --------------------------------------------------------

    @staticmethod
    def _base_of(url: str) -> Optional[str]:
        """``scheme://host`` of an absolute http(s) url, else None."""
        p = urllib.parse.urlparse(url or "")
        if p.scheme in ("http", "https") and p.netloc:
            return f"{p.scheme}://{p.netloc}"
        return None

    async def _discover_mirror_bases(self, max_age: float = MIRROR_DISCOVERY_TTL) -> List[str]:
        """Live serving domains, harvested from the directory landing page.

        ``gotopage.top`` no longer serves the catalogue itself, but its links
        are absolute urls on whichever domain does — the very signal the movie
        path follows when it seeds ``resolved_base``. Reading it here (without
        touching ``resolved_base``: the directory must never become the base
        relative links resolve against) means a mirror move is absorbed
        automatically instead of needing a code change.
        """
        now = time.monotonic()
        if self.mirror_bases and now - self._mirror_checked_at < max_age:
            return list(self.mirror_bases)
        try:
            response = await self.client.get(self.base_url, timeout=MIRROR_DISCOVERY_TIMEOUT)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception as e:  # noqa: BLE001 — discovery is best-effort
            print(f"Mirror discovery failed: {type(e).__name__}: {e}")
            return list(self.mirror_bases)

        directory = self._base_of(self.base_url)
        bases: List[str] = []
        for a in soup.find_all("a", href=True):
            base = self._base_of(a["href"])
            if base and base != directory and base not in bases:
                bases.append(base)
        if bases:
            self.mirror_bases = bases
            self._mirror_checked_at = now
            print(f"Mirror discovery: live domain(s) {bases}")
        return list(self.mirror_bases)

    async def _series_base_candidates(self, refresh: bool = False) -> List[str]:
        """Ordered live-domain candidates for the series listing.

        Last known-good first, then whatever domain the rest of the scraper
        resolved, then the directory's current links, then the static seeds.
        ``resolved_base`` is only *one* candidate: it is empty on a fresh
        process and it can be the directory itself, neither of which is a
        serving mirror.
        """
        directory = self._base_of(self.base_url)
        discovered = await self._discover_mirror_bases(
            0.0 if refresh else MIRROR_DISCOVERY_TTL
        )
        candidates: List[str] = []
        for base in (self.series_base, self.resolved_base, *discovered, *SERIES_SEED_BASES):
            if base and base != directory and base not in candidates:
                candidates.append(base)
        return candidates

    @staticmethod
    def _series_url(base: str, page: int) -> str:
        url = f"{base.rstrip('/')}{SERIES_PATH}"
        return url if page <= 1 else f"{url}?get-page={page}"

    async def _series_listing_soup(self, page: int) -> BeautifulSoup:
        """Fetch the series listing from whichever mirror currently answers.

        Candidates are tried in order and the winner is remembered as
        ``series_base`` (and as ``resolved_base``, since it demonstrably
        serves the site). If every known domain fails the directory is
        re-read once — a moved mirror shows up there before it shows up
        anywhere else.
        """
        errors: List[str] = []
        tried: set = set()

        async def _try(bases: List[str]) -> Optional[BeautifulSoup]:
            for base in bases:
                if base in tried:
                    continue
                tried.add(base)
                try:
                    soup = await self._get_soup(
                        self._series_url(base, page), timeout=SERIES_FETCH_TIMEOUT
                    )
                except Exception as e:  # noqa: BLE001 — try the next mirror
                    errors.append(f"{base} ({type(e).__name__})")
                    continue
                self.series_base = base
                self.resolved_base = base
                return soup
            return None

        soup = await _try(await self._series_base_candidates())
        if soup is None:
            soup = await _try(await self._series_base_candidates(refresh=True))
        if soup is None:
            raise SeriesListingUnavailable(
                "no mirror served the series listing; tried " + ", ".join(errors)
            )
        return soup

    async def get_series_list(self, page: int = 1) -> List[Dict[str, any]]:
        """Series listing page (paginated with the site's ?get-page=N param).

        The domain is resolved dynamically (see ``_series_listing_soup``) so a
        mirror move can no longer break the shelf; the emitted links are
        relative on that page and resolve against the mirror that just served.
        """
        soup = await self._series_listing_soup(page)
        items, seen = [], set()
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            text = a.get_text(strip=True)
            if "web-series" not in href or not text or self._is_nav_junk(text):
                continue
            # Pagination anchors point back at the listing path itself (the
            # "»" next-page arrow reached the shelf as a posterless tile).
            if "get-page=" in href:
                continue
            link = await self._resolve_url(href)
            key = path_key(link)
            if key in seen:
                continue
            seen.add(key)
            items.append({"title": text, "link": link})
        return items

    async def get_seasons(self, series_url: str) -> Dict[str, any]:
        """Series page: season folders (+ poster/description metadata)."""
        data = await self.get_qualities(canonical_page_url(series_url))
        seasons, episodes = [], []
        for item in data["qualities"]:
            href = item["link"].lower()
            if "epi-" in href or "/download/" in href:
                episodes.append(item)  # single-season series lists episodes directly
            elif "season" in href or "season" in item["name"].lower():
                seasons.append(item)
        return {"seasons": seasons, "episodes": episodes, "meta": data["meta"]}

    async def _episode_folders(self, page_url: str) -> List[str]:
        """Sub-folders of a season page that may hold the episode files.

        The mirror wraps a season in per-quality folders (season page →
        1080p/720p folder → …-epi-NN files), so the season page itself has no
        episode links. Highest quality first — but the caller only trusts a
        folder that actually yields episodes.
        """
        try:
            data = await self.get_qualities(page_url)
        except Exception as e:  # noqa: BLE001 — detail lookup is best-effort
            print(f"Season folder lookup failed: {e}")
            return []
        folders = [q["link"] for q in data.get("qualities", []) if q.get("link")]
        priority = ("1080", "720", "480", "360", "original", "hd")
        ordered: List[str] = []
        for p in priority:
            ordered += [f for f in folders if p in f.lower() and f not in ordered]
        ordered += [f for f in folders if f not in ordered]
        return ordered

    async def get_episodes(
        self, season_url: str, pages: int = 10, depth: int = 0
    ) -> List[Dict[str, any]]:
        """Season page: episode links, aggregated over pagination.

        The site lists newest-first; results are returned oldest-first with a
        numeric ``episode`` field parsed from the /download/…-epi-N/ slug.

        When the page handed in carries no episode links of its own it is a
        quality container, not a season (what the current mirror serves); the
        folders beneath it are then walked once, best quality first, so the
        frontend's listing → seasons → episodes flow keeps working.
        """
        base = canonical_page_url(season_url)
        by_key = {}
        for page in range(1, pages + 1):
            url = base if page == 1 else f"{base}{'&' if '?' in base else '?'}page={page}"
            try:
                soup = await self._get_soup(url)
            except Exception as e:
                print(f"Episode page {page} failed: {e}")
                break
            found = 0
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                text = a.get_text(strip=True)
                if ("epi-" not in href and "/download/" not in href) or not text:
                    continue
                if self._is_nav_junk(text):
                    continue
                link = await self._resolve_url(href)
                key = path_key(link)
                if not key or key in by_key:
                    continue
                m = re.search(r"epi-(\d+)", href)
                by_key[key] = {
                    "title": text.replace("Moviesda.Mobi - ", "").strip(),
                    "link": link,
                    "episode": int(m.group(1)) if m else None,
                }
                found += 1
            if found == 0:
                break
            await asyncio.sleep(0.2)

        if not by_key and depth == 0:
            for folder in await self._episode_folders(base):
                episodes = await self.get_episodes(folder, pages=pages, depth=depth + 1)
                if episodes:
                    return episodes

        episodes = list(by_key.values())
        episodes.sort(key=lambda e: (e["episode"] is None, e["episode"] or 0))
        return episodes

    async def resolve_episode(self, episode_url: str) -> Optional[Dict[str, str]]:
        """Episode page -> best direct stream (tries every server in turn)."""
        servers = await self.get_servers(canonical_page_url(episode_url))
        for srv in servers:
            link = await self.resolve_final_link(srv["link"], depth=0)
            if link:
                return {"stream_url": link, "server_label": srv["server"]}
        return None
