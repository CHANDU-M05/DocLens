import requests
from bs4 import BeautifulSoup
import trafilatura
from urllib.parse import urljoin, urlparse, urlunparse, urlencode, parse_qsl
from urllib.robotparser import RobotFileParser
import logging
import re
import time
import json
import html2text
import sqlite3
import hashlib
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger(__name__)

CACHE_DB = Path(__file__).parent.parent / ".crawl_cache.db"
CACHE_TTL_HOURS = 24


def _get_cache_db():
    conn = sqlite3.connect(CACHE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crawl_cache (
            url_hash TEXT PRIMARY KEY, url TEXT NOT NULL,
            content TEXT, structure TEXT, title TEXT, cached_at REAL NOT NULL
        )
    """)
    conn.commit()
    return conn

def _url_hash(url): return hashlib.sha256(url.encode()).hexdigest()

def normalize_url(url):
    parsed = urlparse(url)
    clean = parsed._replace(query=urlencode(sorted(parse_qsl(parsed.query))), fragment="")
    return urlunparse(clean)

class RobotsCache:
    def __init__(self): self._cache = {}
    def is_allowed(self, url, user_agent="*"):
        domain = urlparse(url).netloc
        if domain not in self._cache:
            rp = RobotFileParser()
            rp.set_url(f"{urlparse(url).scheme}://{domain}/robots.txt")
            try: rp.read()
            except Exception: self._cache[domain] = None; return True
            self._cache[domain] = rp
        rp = self._cache[domain]
        return True if rp is None else rp.can_fetch(user_agent, url)

class Crawler:
    def __init__(self, max_pages=100, delay=0.5, max_depth=2, use_cache=True, cache_ttl_hours=CACHE_TTL_HOURS):
        self.max_pages = max_pages
        self.delay = delay
        self.max_depth = max_depth
        self.use_cache = use_cache
        self.cache_ttl_hours = cache_ttl_hours
        self.visited_urls = set()
        self.queue = []
        self.content_map = {}
        self.url_hierarchy = defaultdict(list)
        self.url_titles = {}
        self.url_depths = {}
        self.url_metadata = {}
        self.url_structure = {}
        self.robots = RobotsCache()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "DocLens-Crawler/1.0"})
        self.h2t = html2text.HTML2Text()
        self.h2t.body_width = 0
        self.h2t.ignore_links = False
        self.h2t.ignore_images = True
        self.h2t.ignore_tables = False
        self.h2t.unicode_snob = True
        self.h2t.mark_code = True

    def _cache_get(self, url):
        if not self.use_cache: return None
        try:
            conn = _get_cache_db()
            row = conn.execute("SELECT content,structure,title,cached_at FROM crawl_cache WHERE url_hash=?", (_url_hash(url),)).fetchone()
            conn.close()
            if not row: return None
            content, sj, title, cached_at = row
            if (time.time()-cached_at)/3600 > self.cache_ttl_hours: return None
            return {"content": content, "structure": json.loads(sj) if sj else {}, "title": title or ""}
        except Exception as e:
            logger.warning(f"Cache read error: {e}"); return None

    def _cache_set(self, url, content, structure, title):
        if not self.use_cache: return
        try:
            conn = _get_cache_db()
            conn.execute("INSERT OR REPLACE INTO crawl_cache (url_hash,url,content,structure,title,cached_at) VALUES (?,?,?,?,?,?)",
                (_url_hash(url), url, content, json.dumps(structure), title, time.time()))
            conn.commit(); conn.close()
        except Exception as e: logger.warning(f"Cache write error: {e}")

    def _fetch(self, url, timeout=10):
        for attempt in range(3):
            try:
                resp = self._session.get(url, timeout=timeout)
                if resp.status_code == 429:
                    time.sleep(int(resp.headers.get("Retry-After", 10))); continue
                if resp.status_code in (403, 401): return None
                if resp.status_code >= 500: time.sleep(2**attempt); continue
                return resp
            except requests.exceptions.ConnectionError: time.sleep(2**attempt)
            except requests.exceptions.Timeout: time.sleep(2**attempt)
        return None

    def is_valid_url(self, url, base_url):
        if not url: return False
        base_domain = urlparse(base_url).netloc
        url_domain = urlparse(url).netloc
        skip_ext = ['.pdf','.jpg','.png','.gif','.jpeg','.svg','.mp4','.zip','.css','.js','.ico','.woff','.woff2','.ttf','.xml']
        if any(url.lower().split('?')[0].endswith(e) for e in skip_ext): return False
        skip_patterns = ['/cdn-cgi/','/wp-content/','/wp-includes/','/static/','/assets/','/_next/']
        if any(p in url for p in skip_patterns): return False
        return url_domain == base_domain or url_domain.endswith('.'+base_domain)

    def extract_clean_text(self, url):
        cached = self._cache_get(url)
        if cached:
            self.url_titles[url] = cached["title"]
            self.url_structure[url] = cached["structure"]
            return cached
        resp = self._fetch(url)
        if resp is None: return {"content":"","structure":{},"title":""}
        soup = BeautifulSoup(resp.text, 'html.parser')
        title = (soup.title.string or "Untitled").strip()
        self.url_titles[url] = title
        self.url_metadata[url] = {"url": url, "timestamp": time.time()}
        main = self._identify_main_content(soup)
        doc_structure = self._extract_document_structure(main or soup)
        self.url_structure[url] = doc_structure
        enhanced_text = self._generate_structured_text(main or soup)
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                traf = trafilatura.extract(downloaded, include_links=True, include_formatting=True)
                if traf and len(traf) > len(enhanced_text)*0.7: enhanced_text = traf
        except Exception: pass
        self._cache_set(url, enhanced_text, doc_structure, title)
        return {"content": enhanced_text, "structure": doc_structure, "title": title}

    def _identify_main_content(self, soup):
        for sel in ['main','article','[role="main"]','.main-content','#main-content','#content','#main','.content','.page-content']:
            els = soup.select(sel)
            if els: return max(els, key=lambda e: len(e.get_text())) if len(els)>1 else els[0]
        candidates = [d for d in soup.find_all('div') if len(d.get_text().strip())>200]
        return max(candidates, key=lambda e: len(e.get_text())) if candidates else None

    def _extract_document_structure(self, element):
        structure = {"headings":[],"lists":[],"tables":[],"code_blocks":[]}
        if not element: return structure
        for i in range(1,7):
            for h in element.find_all(f'h{i}'):
                structure["headings"].append({"level":i,"text":h.get_text().strip(),"id":h.get('id','')})
        for lst in element.find_all(['ul','ol']):
            structure["lists"].append({"type":lst.name,"items":[{"text":li.get_text().strip()} for li in lst.find_all('li',recursive=False)]})
        for tbl in element.find_all('table'):
            structure["tables"].append({"headers":[th.get_text().strip() for th in tbl.find_all('th')],"rows":[[td.get_text().strip() for td in tr.find_all('td')] for tr in tbl.find_all('tr') if tr.find_all('td')]})
        for code in element.find_all(['pre','code']):
            if code.name=='code' and code.parent and code.parent.name=='pre': continue
            structure["code_blocks"].append({"type":code.name,"text":code.get_text()})
        return structure

    def _generate_structured_text(self, element):
        if not element: return ""
        for tag in element.find_all(["script","style","nav","footer","header","aside"]): tag.extract()
        return self.h2t.handle(str(element))

    def get_links(self, url, current_depth):
        if current_depth >= self.max_depth: return []
        resp = self._fetch(url)
        if resp is None: return []
        soup = BeautifulSoup(resp.text, 'html.parser')
        links = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            if not href or href.startswith('javascript:') or href=='#': continue
            full_url = normalize_url(urljoin(url, href))
            if not self.is_valid_url(full_url, url): continue
            if not self.robots.is_allowed(full_url): continue
            if full_url in self.visited_urls: continue
            links.append(full_url)
            self.url_hierarchy[url].append(full_url)
            self.url_depths[full_url] = current_depth+1
            link_text = a.get_text(strip=True)
            if link_text and full_url not in self.url_titles: self.url_titles[full_url] = link_text
        return links

    def _prioritize_urls(self, url_list):
        doc_patterns = ['article','doc','help','guide','faq','tutorial','support','manual','reference']
        return sorted(url_list, key=lambda url: 0 if any(p in url.lower() for p in doc_patterns) else url.count('/'))

    def crawl(self, start_url):
        start_url = normalize_url(start_url)
        self.visited_urls = set()
        self.queue = [(start_url, 0)]
        self.content_map = {}
        self.url_hierarchy = defaultdict(list)
        self.url_titles = {}
        self.url_depths = {start_url: 0}
        self.url_metadata = {}
        self.url_structure = {}
        while self.queue and len(self.visited_urls) < self.max_pages:
            current_url, current_depth = self.queue.pop(0)
            if current_url in self.visited_urls: continue
            logger.info(f"Crawling [depth={current_depth}]: {current_url}")
            self.visited_urls.add(current_url)
            time.sleep(self.delay)
            result = self.extract_clean_text(current_url)
            if result["content"].strip(): self.content_map[current_url] = result["content"]
            if current_depth < self.max_depth:
                links = self._prioritize_urls(self.get_links(current_url, current_depth))
                queued = {u for u,_ in self.queue}
                for link in links:
                    if link not in self.visited_urls and link not in queued:
                        self.queue.append((link, current_depth+1))
        logger.info(f"Done. {len(self.visited_urls)} pages, {len(self.content_map)} with content.")
        return {"content":self.content_map,"hierarchy":dict(self.url_hierarchy),"titles":self.url_titles,"depths":self.url_depths,"metadata":self.url_metadata,"structure":self.url_structure}

    def crawl_multiple(self, urls):
        combined = {"content":{},"hierarchy":{},"titles":{},"depths":{},"metadata":{},"structure":{}}
        for url in urls:
            result = self.crawl(url)
            for key in combined: combined[key].update(result[key])
        return combined
