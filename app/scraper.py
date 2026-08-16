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

import httpx
from bs4 import BeautifulSoup
from typing import Optional, List, Dict
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

    async def _get_soup(self, url: str) -> BeautifulSoup:
        print(f"Fetching: {url}")
        response = await self.client.get(url)
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

    async def resolve_final_link(self, server_url: str, depth: int = 0) -> Optional[str]:
        """Level 6+: Recursively follow redirect/server pages to get final media link."""
        if depth > 3:
            print("Max depth reached in resolving link.")
            return None

        try:
            print(f"Resolving (Depth {depth}): {server_url}")
            soup = await self._get_soup(server_url)

            # 1. Direct download button (.mp4/.mkv) — success
            for a in soup.find_all("a"):
                href = a.get("href", "")
                if href.endswith(".mp4") or href.endswith(".mkv"):
                    return await self._resolve_url(href)

            # 2. "Download Server" buttons — recurse
            for a in soup.find_all("a"):
                text = a.get_text(strip=True)
                href = a.get("href", "")
                if ("download server" in text.lower() or "server" in text.lower()) and "home" not in text.lower():
                     next_link = await self._resolve_url(href)
                     if next_link != server_url:
                         print(f"Following recursive link: {text} -> {next_link}")
                         result = await self.resolve_final_link(next_link, depth + 1)
                         if result: return result

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
