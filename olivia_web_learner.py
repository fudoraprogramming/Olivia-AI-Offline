"""
olivia_web_learner.py — v3: round-robin + UA rotation + blacklist + JS mode.

NEW in v3:
  * User-Agent rotation: cycles 5 modern browser strings to bypass 403s
  * Auto-blacklist: after 3 failures a URL goes to dead_urls.json
                     and is skipped silently. Re-tested every 30 days.
  * JS-mode (opt-in): for sites that yield 0 chars or always 403,
                       fall back to Playwright headless Chromium.
                       Set self.js_mode = True to enable.
"""

import gc
import hashlib
import itertools
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote_plus

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

try:
    from bs4 import BeautifulSoup
    BS4_OK = True
except ImportError:
    BS4_OK = False

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False


# ----- Multiple modern UAs to rotate through -----
USER_AGENTS = [
    # Chrome on Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Firefox on Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_1) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    # Mobile Safari iPhone
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 "
    "Safari/604.1",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

MAX_HTML_BYTES = 1_000_000


# ----- ad/noise filter -----
AD_PATTERNS = [
    r"cookie\s*(policy|consent|notice)", r"privacy\s*policy",
    r"terms\s*(of|&)\s*(service|use|conditions)", r"sign\s*(up|in|into)",
    r"subscribe\s*(to|now|here)", r"newsletter", r"advertisement",
    r"sponsored\s*(content|post|by)", r"buy\s*now", r"shop\s*now",
    r"add\s*to\s*cart", r"free\s*trial", r"limited\s*time\s*offer",
    r"loading\s*\.\.\.", r"please\s*enable\s*javascript",
    r"accept\s*(all)?\s*cookies", r"we\s*use\s*cookies",
    r"gdpr", r"ccpa", r"powered\s*by",
]
_AD_RE = re.compile("|".join(AD_PATTERNS), re.IGNORECASE)


def is_noise(text):
    t = text.strip()
    if len(t) < 15:
        return True
    return bool(_AD_RE.search(t.lower()))


def filter_lines(lines):
    out = []
    for line in lines:
        line = line.strip()
        if not line or len(line) < 20 or is_noise(line):
            continue
        out.append(line)
    return out


EMOTION_LEXICON = {
    "joy":      ["happy","glad","joy","excited","wonderful","amazing","love",
                 "delighted","thrilled","grateful"],
    "sadness":  ["sad","unhappy","depressed","miserable","heartbroken",
                 "lonely","grief","sorrow","despair"],
    "anger":    ["angry","furious","rage","hate","frustrated","outraged",
                 "irritated","resentment"],
    "fear":     ["afraid","scared","terrified","anxious","worried","nervous",
                 "panic","dread"],
    "surprise": ["surprised","shocked","amazed","astonished","unexpected"],
    "disgust":  ["disgusted","revolting","gross","nasty","repulsive"],
    "trust":    ["trust","believe","faith","reliable","honest","loyal"],
}

def analyze_emotions(text):
    low = text.lower()
    out = {}
    for emo, words in EMOTION_LEXICON.items():
        n = sum(1 for w in words if w in low)
        if n:
            out[emo] = n
    return out


# ============================================================================
# Dead-URL blacklist
# ============================================================================

DEAD_URL_FILE = Path.home() / "olivia_v8" / "memory" / "dead_urls.json"
DEAD_THRESHOLD = 3                # consecutive failures
RETRY_AFTER_DAYS = 30             # give dead URLs a second chance after this


class DeadUrlTracker:
    def __init__(self, path=None):
        self.path = Path(path) if path else DEAD_URL_FILE
        self.failures = {}    # url -> count
        self.dead = {}        # url -> {ts_first_dead, last_err}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                self.failures = d.get("failures", {})
                self.dead = d.get("dead", {})
            except Exception:
                pass

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "failures": self.failures,
                "dead": self.dead,
            }))
        except Exception:
            pass

    def is_dead(self, url):
        info = self.dead.get(url)
        if not info:
            return False
        # retry after RETRY_AFTER_DAYS
        try:
            ts = info.get("ts_first_dead", 0)
            if (time.time() - ts) / 86400 > RETRY_AFTER_DAYS:
                # resurrect — clear it so we try again
                del self.dead[url]
                self.failures.pop(url, None)
                self._save()
                return False
        except Exception:
            pass
        return True

    def record_failure(self, url, err):
        self.failures[url] = self.failures.get(url, 0) + 1
        if self.failures[url] >= DEAD_THRESHOLD:
            self.dead[url] = {
                "ts_first_dead": time.time(),
                "last_err": str(err)[:80],
                "fails": self.failures[url],
            }
        self._save()

    def record_success(self, url):
        if url in self.failures:
            del self.failures[url]
        if url in self.dead:
            del self.dead[url]
        self._save()

    def stats(self):
        return {
            "tracked_failing": len(self.failures),
            "dead": len(self.dead),
        }


# ============================================================================
# WebLearner v3
# ============================================================================

class WebLearner:
    def __init__(self, memory, thought_callback=None, js_mode=False):
        self.memory = memory
        self.think = thought_callback or (lambda msg: None)
        self.session = None
        self.is_crawling = False
        self.js_mode = js_mode and PLAYWRIGHT_OK
        self.dead_urls = DeadUrlTracker()
        # rotating UA iterator
        self._ua_cycle = itertools.cycle(USER_AGENTS)
        # playwright handles (lazy)
        self._pw = None
        self._pw_browser = None

        if REQUESTS_OK:
            self.session = requests.Session()
            self.session.headers.update(BASE_HEADERS)
            retry = Retry(total=0)
            ad = HTTPAdapter(max_retries=retry, pool_connections=4,
                             pool_maxsize=4)
            self.session.mount("http://",  ad)
            self.session.mount("https://", ad)

    def stop(self):
        self.is_crawling = False

    def set_js_mode(self, enabled):
        self.js_mode = bool(enabled) and PLAYWRIGHT_OK
        return self.js_mode

    def shutdown_browser(self):
        if self._pw_browser:
            try:
                self._pw_browser.close()
            except Exception:
                pass
            self._pw_browser = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    def _next_ua(self):
        return next(self._ua_cycle)

    # ---- HTTP fetch (interruptible, UA-rotating) -----------------------

    def fetch_page(self, url, filter_ads=True, timeout=5):
        if self.dead_urls.is_dead(url):
            return "", url, "", "blacklisted"
        if not self.session:
            return "", "", "", "requests not installed"
        try:
            self.session.headers["User-Agent"] = self._next_ua()
            r = self.session.get(url, timeout=timeout, allow_redirects=True,
                                 stream=True)
            r.raise_for_status()
            chunks = []
            received = 0
            for chunk in r.iter_content(chunk_size=32_768):
                if not self.is_crawling:
                    r.close()
                    return "", url, "", "interrupted"
                if chunk:
                    chunks.append(chunk)
                    received += len(chunk)
                    if received > MAX_HTML_BYTES:
                        r.close()
                        return "", url, "", "page_too_large"
            r.close()
            raw = b"".join(chunks)
            try:
                html = raw.decode(r.encoding or "utf-8", errors="replace")
            except Exception:
                html = raw.decode("utf-8", errors="replace")

            text, title = self._extract(html, filter_ads)

            # If we got near-empty content, AND js_mode is on, retry with browser
            if self.js_mode and len(text) < 200:
                self.think("  (low content, trying JS browser...)")
                jt, jtitle = self._fetch_js(url)
                if jt and len(jt) > len(text):
                    text, title = jt, (jtitle or title)

            self.dead_urls.record_success(url)
            return text, r.url, title, None

        except requests.exceptions.Timeout:
            self.dead_urls.record_failure(url, "timeout")
            return "", url, "", "timeout"
        except requests.exceptions.RequestException as e:
            errstr = str(e)[:80]
            # 403/404/410 → if js_mode try the browser as a fallback
            if self.js_mode and any(c in errstr for c in ("403", "404", "410")):
                self.think("  (HTTP " + errstr[:8] + ", trying JS browser...)")
                jt, jtitle = self._fetch_js(url)
                if jt and len(jt) > 200:
                    self.dead_urls.record_success(url)
                    return jt, url, jtitle or "", None
            self.dead_urls.record_failure(url, errstr)
            return "", url, "", errstr
        except Exception as e:
            errstr = str(e)[:80]
            self.dead_urls.record_failure(url, errstr)
            return "", url, "", errstr

    def _extract(self, html, filter_ads=True):
        if not BS4_OK:
            return html, ""
        soup = BeautifulSoup(html, "html.parser")
        title = (soup.find("title").get_text().strip()
                 if soup.find("title") else "")
        for tag in soup(["script","style","nav","footer","header","aside",
                         "noscript","iframe","svg","form","button",
                         "input","select"]):
            tag.decompose()
        main = soup.find("article") or soup.find("main") or soup.find("body")
        parts = []
        if main:
            for p in main.find_all(
                ["p","h1","h2","h3","h4","h5","h6","li","blockquote",
                 "td","pre","code","div"], recursive=True):
                t = p.get_text().strip()
                if t and len(t) > 20:
                    parts.append(t)
        else:
            parts = [soup.get_text(separator="\n", strip=True)]
        if filter_ads:
            parts = filter_lines(parts)
        text = "\n".join(parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r" {2,}", " ", text)
        return text, title

    # ---- Playwright JS fetcher (opt-in, slow but powerful) -------------

    def _ensure_browser(self):
        if not PLAYWRIGHT_OK:
            return False
        if self._pw_browser:
            return True
        try:
            self._pw = sync_playwright().start()
            self._pw_browser = self._pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox"])
            return True
        except Exception as e:
            self.think("  (browser launch failed: " + str(e)[:60] + ")")
            return False

    def _fetch_js(self, url, timeout=15):
        if not self._ensure_browser():
            return "", ""
        try:
            ctx = self._pw_browser.new_context(user_agent=self._next_ua())
            page = ctx.new_page()
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            html = page.content()
            ctx.close()
            text, title = self._extract(html, filter_ads=True)
            return text, title
        except Exception as e:
            self.think("  (JS fetch error: " + str(e)[:60] + ")")
            return "", ""

    # ---- single-URL ingest ---------------------------------------------

    def learn_from_url(self, url):
        self.is_crawling = True
        self.think("Fetching: " + url)
        text, final_url, title, err = self.fetch_page(url)
        if err:
            return False, err
        if not text or len(text) < 100:
            return False, "too little content"
        h = hashlib.sha256(text[:5000].encode("utf-8")).hexdigest()
        if not self.memory.is_web_page_new(url, h):
            return False, "no changes"
        if not self.is_crawling:
            return False, "interrupted"
        emos = analyze_emotions(text)
        added = self.memory.add_source(
            path=url, title=title or url, kind="web",
            text=text[:50000], content_hash=h,
            meta={"emotions": emos})
        self.memory.mark_web_page_seen(url, h, title, len(text))
        return True, str(added.chunks_added) + " chunks"

    # ---- round-robin crawl ---------------------------------------------

    def crawl_round_robin(self, sites, state_file=None,
                          max_pages_per_click=200,
                          max_seconds_per_click=300,
                          max_pages_for=None,
                          pages_per_site_per_cycle=3,
                          callback=None):
        self.is_crawling = True
        cb = callback or (lambda *a, **kw: None)

        if state_file is None:
            state_file = Path.home() / "olivia_v8" / "memory" / "crawl_state.json"
        state_file = Path(state_file)

        state = {"site_idx": 0, "cycle": 0, "site_queues": {},
                 "total_pages": 0, "last_save": time.time()}
        if state_file.exists():
            try:
                state.update(json.loads(state_file.read_text()))
            except Exception:
                pass

        def save_state():
            try:
                state_file.parent.mkdir(parents=True, exist_ok=True)
                state_file.write_text(json.dumps(state)[:10_000_000])
            except Exception:
                pass

        t0 = time.time()
        pages_this_click = 0
        new_this_click   = 0
        sites_touched    = set()
        blacklisted_skipped = 0
        n_sites = len(sites)

        ds = self.dead_urls.stats()
        self.think("ROUND-ROBIN: " + str(n_sites) + " sites, " +
                   "resuming site " + str(state["site_idx"]+1) + ", " +
                   "cycle " + str(state["cycle"]+1) +
                   "  |  blacklist: " + str(ds["dead"]) + " dead, " +
                   str(ds["tracked_failing"]) + " failing" +
                   ("  |  JS mode ON" if self.js_mode else ""))

        while self.is_crawling and pages_this_click < max_pages_per_click:
            elapsed = time.time() - t0
            if elapsed > max_seconds_per_click:
                self.think("hit " + str(max_seconds_per_click) + "s time cap")
                break

            site_idx = state["site_idx"] % n_sites
            if site_idx == 0 and state["site_idx"] > 0:
                state["cycle"] += 1
                self.think("completed cycle " + str(state["cycle"]) +
                           " of all sites")

            url, cat, prio, notes = sites[site_idx]
            domain = urlparse(url).netloc

            sq = state["site_queues"].get(url)
            if sq is None:
                sq = [url]
                state["site_queues"][url] = sq

            if not sq:
                state["site_idx"] += 1
                continue

            sites_touched.add(url)

            for _ in range(pages_per_site_per_cycle):
                if not self.is_crawling:
                    break
                if not sq:
                    break
                if pages_this_click >= max_pages_per_click:
                    break
                if time.time() - t0 > max_seconds_per_click:
                    break

                page_url = sq.pop(0)
                if self.dead_urls.is_dead(page_url):
                    blacklisted_skipped += 1
                    continue
                pages_this_click += 1

                text, final_url, title, err = self.fetch_page(page_url)
                if not self.is_crawling:
                    break
                if err or not text or len(text) < 100:
                    cb(page_url, title or "", 0, pages_this_click,
                       is_new=False, error=err or "no content",
                       site_idx=site_idx, n_sites=n_sites, cat=cat)
                    continue

                h = hashlib.sha256(text[:5000].encode("utf-8")).hexdigest()
                is_new = self.memory.is_web_page_new(page_url, h)

                if is_new and self.is_crawling:
                    self.memory.add_source(
                        path=page_url, title=title or page_url, kind="web",
                        text=text[:50000], content_hash=h)
                    self.memory.mark_web_page_seen(
                        page_url, h, title, len(text))
                    new_this_click += 1
                    state["total_pages"] += 1
                cb(page_url, title, len(text), pages_this_click,
                   is_new=is_new, site_idx=site_idx, n_sites=n_sites, cat=cat)

                if BS4_OK and self.is_crawling:
                    try:
                        r = self.session.get(page_url, timeout=5)
                        soup = BeautifulSoup(r.text[:500_000], "html.parser")
                        added_links = 0
                        for a in soup.find_all("a", href=True):
                            if added_links >= 30 or not self.is_crawling:
                                break
                            full = urljoin(page_url, a["href"])
                            if urlparse(full).netloc != domain:
                                continue
                            if full in sq:
                                continue
                            if self.dead_urls.is_dead(full):
                                continue
                            if any(full.lower().endswith(ext) for ext in
                                   (".pdf",".jpg",".png",".gif",".mp4",".mp3",
                                    ".zip",".tar",".gz",".css",".js",".ico",
                                    ".woff",".woff2",".ttf",".webp",".svg")):
                                continue
                            sq.append(full)
                            added_links += 1
                    except Exception:
                        pass

                time.sleep(0.3)

            state["site_idx"] += 1
            if time.time() - state["last_save"] > 5:
                save_state()
                state["last_save"] = time.time()

        save_state()
        was_stopped = not self.is_crawling
        self.is_crawling = False
        gc.collect()
        ds = self.dead_urls.stats()
        return {
            "pages_this_click":     pages_this_click,
            "new_this_click":       new_this_click,
            "sites_touched":        len(sites_touched),
            "blacklisted_skipped":  blacklisted_skipped,
            "total_pages_ever":     state["total_pages"],
            "cycle":                state["cycle"],
            "site_idx":             state["site_idx"] % n_sites,
            "n_sites":              n_sites,
            "stopped_by_user":      was_stopped,
            "dead_urls":            ds["dead"],
            "failing_urls":         ds["tracked_failing"],
        }

    def crawl_all_sites(self, sites, max_pages_for=None, callback=None,
                        between_sites=2.0):
        r = self.crawl_round_robin(sites, max_pages_for=max_pages_for,
                                   callback=callback)
        return r.get("new_this_click", 0)

    def search_and_learn(self, topic, depth=3):
        wiki = "https://en.wikipedia.org/wiki/" + topic.replace(" ", "_")
        ok, msg = self.learn_from_url(wiki)
        if ok:
            return True, "learned about '" + topic + "' from Wikipedia"
        ddg = "https://html.duckduckgo.com/html/?q=" + quote_plus(topic)
        text, _, _, _ = self.fetch_page(ddg, filter_ads=False)
        if text and BS4_OK:
            soup = BeautifulSoup(text, "html.parser")
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if href.startswith("http") and "duckduckgo" not in href:
                    ok, msg = self.learn_from_url(href)
                    if ok:
                        return True, "learned about '" + topic + "' from " + href
        return False, "couldn't find '" + topic + "'"
