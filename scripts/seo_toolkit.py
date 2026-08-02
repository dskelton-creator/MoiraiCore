#!/usr/bin/env python3
"""
MoiraiCore — SEO Toolkit
Comprehensive SEO analysis toolkit — runs entirely locally, zero API costs.

Modules:
1. keyword_research  — TF-IDF keyword extraction, related terms
2. content_analyzer  — SEO scoring, readability, density, structure
3. site_crawler      — Crawl sites, extract SEO data, find broken links
4. competitor_analysis — Compare pages head-to-head
5. schema_generator  — JSON-LD schema markup for 6+ types
6. backlink_checker  — Link analysis (internal/external, nofollow, anchors)
7. seo_audit         — Combined audit with scoring & recommendations

Usage:
    from seo_toolkit import SEOAnalyzer
    seo = SEOAnalyzer()
    result = seo.content_analyze("https://example.com")
    result = seo.keyword_research("Your content text here", top_n=20)
    result = seo.site_crawl("https://example.com", max_pages=50)
    result = seo.competitor_compare("https://yoursite.com", "https://competitor.com")
    result = seo.schema_generate("article", {"headline": "...", "author": "..."})
    result = seo.backlink_check("https://example.com")
    result = seo.full_audit("https://example.com")
"""

import hashlib
import json
import math
import re
import time
from collections import Counter
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

# ── Optional imports with graceful fallback ──
try:
    import nltk
    from nltk.corpus import stopwords
    from nltk.tokenize import word_tokenize, sent_tokenize
    from nltk.stem import WordNetLemmatizer
    _NLTK_OK = True
except ImportError:
    _NLTK_OK = False

try:
    from textblob import TextBlob
    _TEXTBLOB_OK = True
except ImportError:
    _TEXTBLOB_OK = False

try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_OK = True
except ImportError:
    _PLAYWRIGHT_OK = False


def _render_page(url: str, timeout: int = 30) -> dict:
    """
    Render a URL with headless Chromium (Playwright) and return the fully
    client-side-rendered HTML. Required for Cloudflare-gated pages and JS SPAs
    whose SEO content is produced by JavaScript and absent from static HTML.

    Returns {"html": <str>, "url": <final url>} on success, or {"error": <msg>}.
    """
    if not _PLAYWRIGHT_OK:
        return {"error": "Playwright not installed (run: pip install playwright && playwright install chromium)"}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            # Realistic desktop context so Cloudflare challenge is less likely to trigger.
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
                viewport={"width": 1366, "height": 768},
                locale="en-AU",
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            # Give any late JS (helmet, lazy content) a moment to settle.
            page.wait_for_timeout(1500)
            html = page.content()
            final_url = page.url
            browser.close()
            return {"html": html, "url": final_url}
    except Exception as e:
        return {"error": "Render failed: %s" % str(e)}


# ═══════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════

class _HTMLTextExtractor(HTMLParser):
    """Extract visible text from HTML, ignoring script/style/nav/footer."""
    SKIP_TAGS = {"script", "style", "nav", "footer", "header", "aside", "noscript", "iframe"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self._parts = []
        self._current_tag = ""

    def handle_starttag(self, tag, attrs):
        self._current_tag = tag
        if tag in self.SKIP_TAGS:
            self._skip += 1
        elif tag in ("br", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"):
            self._parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self._skip -= 1
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "section"):
            self._parts.append(" ")

    def handle_data(self, data):
        if self._skip <= 0:
            self._parts.append(data)

    def get_text(self):
        return " ".join(self._parts)


def _extract_text(html: str) -> str:
    """Extract visible text from HTML."""
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(html)
        text = extractor.get_text()
    except Exception:
        # Fallback: use BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = soup.get_text(separator=" ")
    # Clean up whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tokenize(text: str) -> list[str]:
    """Tokenize text into words, removing stopwords."""
    words = re.findall(r"[a-zA-Z][a-zA-Z'-]{1,}", text.lower())
    if _NLTK_OK:
        try:
            stops = set(stopwords.words("english"))
        except LookupError:
            nltk.download("stopwords", quiet=True)
            nltk.download("punkt", quiet=True)
            nltk.download("punkt_tab", quiet=True)
            nltk.download("wordnet", quiet=True)
            stops = set(stopwords.words("english"))
        words = [w for w in words if w not in stops and len(w) > 2]
    else:
        # Basic stopword list
        basic_stops = {
            "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
            "her", "was", "one", "our", "out", "has", "have", "been", "were", "they",
            "their", "what", "when", "where", "which", "this", "that", "with", "from",
            "will", "would", "there", "these", "than", "then", "them", "into", "some",
            "could", "other", "about", "more", "just", "also", "only", "very", "much",
            "such", "each", "make", "like", "over", "such", "take", "come", "its",
            "it's", "i'm", "i've", "don't", "doesn't", "didn't", "isn't", "aren't",
            "wasn't", "weren't", "won't", "wouldn't", "shouldn't", "couldn't",
        }
        words = [w for w in words if w not in basic_stops and len(w) > 2]
    return words


def _sentences(text: str) -> list[str]:
    """Split text into sentences."""
    if _NLTK_OK:
        try:
            return sent_tokenize(text)
        except LookupError:
            nltk.download("punkt", quiet=True)
            nltk.download("punkt_tab", quiet=True)
            return sent_tokenize(text)
    # Fallback: simple split
    return [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]


def _readability_score(text: str) -> dict:
    """Calculate Flesch-Kincaid readability metrics."""
    sents = _sentences(text)
    words = text.split()
    syllables = sum(_count_syllables(w) for w in words)

    num_sentences = max(len(sents), 1)
    num_words = max(len(words), 1)
    num_syllables = max(syllables, 1)

    # Flesch Reading Ease
    flesch = 206.835 - 1.015 * (num_words / num_sentences) - 84.6 * (num_syllables / num_words)
    flesch = max(0, min(100, flesch))

    # Flesch-Kincaid Grade Level
    fk_grade = 0.39 * (num_words / num_sentences) + 11.8 * (num_syllables / num_words) - 15.59
    fk_grade = max(0, fk_grade)

    # Interpret
    if flesch >= 80:
        level = "Very Easy"
        audience = "11 and under"
    elif flesch >= 60:
        level = "Easy"
        audience = "12–14 years"
    elif flesch >= 40:
        level = "Moderate"
        audience = "15–18 years"
    elif flesch >= 20:
        level = "Difficult"
        audience = "University"
    else:
        level = "Very Difficult"
        audience = "Graduate"

    return {
        "flesch_reading_ease": round(flesch, 1),
        "flesch_kincaid_grade": round(fk_grade, 1),
        "avg_words_per_sentence": round(num_words / num_sentences, 1),
        "avg_syllables_per_word": round(num_syllables / num_words, 2),
        "interpretation": level,
        "target_audience": audience,
        "total_words": num_words,
        "total_sentences": num_sentences,
        "total_syllables": num_syllables,
    }


def _detect_rendering_block(data: dict) -> Optional[dict]:
    """
    Detect when the fetched HTML is NOT the real page content — i.e. a JS-rendered
    SPA shell or a Cloudflare/bot-challenge interstitial. In both cases the static
    HTML has almost no visible text and the actual SEO content (title, meta, H1, body)
    is produced client-side by JavaScript, which this toolkit cannot execute.

    Returns a dict describing the block (with `rendering_blocked=True`) or None if the
    page looks like genuine server-rendered HTML.
    """
    html = data.get("html", "") or ""
    html_lower = html.lower()
    word_count = data.get("word_count", 0)
    text = (data.get("text") or "").strip()

    # Markers that strongly indicate a challenge/SPA shell rather than real content.
    cf_markers = (
        "__cf_chl_" in html_lower
        or "cf-chl" in html_lower
        or "challenge-platform" in html_lower
        or "jsd/main.js" in html_lower
    )
    spa_markers = (
        'id="root"' in html_lower
        or 'id="app"' in html_lower
        or "react-helmet" in html_lower
        or "reacthelmet" in html_lower
        or "ng-app" in html_lower
        or "__NEXT_DATA__" in html_lower
        or "__NUXT__" in html_lower
    )

    # Too little real text to analyse meaningfully.
    thin = word_count < 40 and len(text) < 250

    if cf_markers and thin:
        return {
            "rendering_blocked": True,
            "reason": "cloudflare_challenge",
            "detail": (
                "The page is served behind a Cloudflare browser-challenge. The HTML "
                "returned to a non-browser client is an interstitial, not the real site, "
                "so SEO content (title, meta, headings, body) cannot be analysed statically."
            ),
        }
    if spa_markers and thin:
        return {
            "rendering_blocked": True,
            "reason": "spa_shell",
            "detail": (
                "The page is a JavaScript single-page app (SPA). Its SEO content is "
                "rendered client-side and is absent from the static HTML, so it cannot "
                "be analysed without a JavaScript-capable renderer."
            ),
        }
    return None


def _count_syllables(word: str) -> int:
    """Estimate syllable count for a word."""
    word = word.lower().strip()
    if not word:
        return 0
    if len(word) <= 3:
        return 1
    word = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", word)
    word = re.sub(r"^y", "", word)
    groups = re.findall(r"[aeiouy]{1,2}", word)
    return max(len(groups), 1)


def _fetch_url(url: str, timeout: int = 15) -> Optional[dict]:
    """Fetch a URL and return parsed data. Blocks internal/private IPs to prevent SSRF."""
    import ipaddress, socket
    from urllib.parse import urlparse

    # SSRF protection: block internal/private/hostile addresses
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return {"error": "Invalid URL — no hostname", "url": url}
        # Resolve hostname and check if it's a private/internal IP
        try:
            addr = socket.getaddrinfo(hostname, parsed.port or 80, socket.AF_INET, socket.SOCK_STREAM)
            for family, type_, proto, canonname, sockaddr in addr:
                ip = ipaddress.ip_address(sockaddr[0])
                if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                    return {"error": "Blocked: URL resolves to internal/private IP (" + str(ip) + ")", "url": url}
        except (socket.gaierror, ValueError):
            pass  # Let the request fail naturally if DNS fails
    except Exception:
        pass

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            return None
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        return {
            "url": resp.url,
            "status": resp.status_code,
            "html": html,
            "soup": soup,
            "headers": dict(resp.headers),
            "elapsed": resp.elapsed.total_seconds(),
        }
    except Exception as e:
        return {"error": str(e), "url": url}


def _get_page_data(url: str, html: Optional[str] = None) -> dict:
    """Get comprehensive page data from URL or raw HTML.

    If `html` is provided (e.g. a pre-rendered DOM from Playwright), it is parsed
    directly instead of fetching the URL.
    """
    if html is not None:
        try:
            soup = BeautifulSoup(html, "html.parser")
            fetched = {"url": url, "status": 200, "html": html, "soup": soup}
        except Exception as e:
            return {"error": f"Could not parse rendered HTML: {e}", "url": url}
    else:
        fetched = _fetch_url(url)
        if fetched and "error" not in fetched:
            soup = fetched["soup"]
        elif fetched and "error" in fetched and fetched["error"].startswith("Blocked:"):
            # SSRF block — propagate the error, don't fall through to raw HTML parsing
            return {"error": fetched["error"], "url": url}
        else:
            # Try parsing as raw HTML (only for non-URL strings)
            # If it looks like a URL (starts with http), don't treat as HTML
            if url.startswith("http://") or url.startswith("https://"):
                return {"error": "Could not fetch URL: " + (fetched.get("error", "unknown error") if fetched else "fetch failed"), "url": url}
            try:
                soup = BeautifulSoup(url, "html.parser")
                fetched = {"url": "", "status": 0, "html": url, "soup": soup}
            except Exception:
                return {"error": f"Could not parse: {url}", "raw_error": fetched.get("error", "") if fetched else ""}

    # Extract all SEO-relevant data
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    meta_desc = ""
    meta_tag = soup.find("meta", attrs={"name": "description"})
    if meta_tag:
        meta_desc = meta_tag.get("content", "")

    meta_keywords = ""
    kw_tag = soup.find("meta", attrs={"name": "keywords"})
    if kw_tag:
        meta_keywords = kw_tag.get("content", "")

    canonical = ""
    canon_tag = soup.find("link", attrs={"rel": "canonical"})
    if canon_tag:
        canonical = canon_tag.get("href", "")

    robots = ""
    robots_tag = soup.find("meta", attrs={"name": "robots"})
    if robots_tag:
        robots = robots_tag.get("content", "")

    og_title = ""
    og_desc = ""
    og_image = ""
    og_type = ""
    for prop, var in [("title", "og_title"), ("description", "og_desc"), ("image", "og_image"), ("type", "og_type")]:
        tag = soup.find("meta", attrs={"property": f"og:{prop}"})
        if tag:
            locals()[var] = tag.get("content", "")

    # Headings
    headings = {}
    for level in range(1, 7):
        tag = f"h{level}"
        headings[tag] = [h.get_text(strip=True) for h in soup.find_all(tag)]

    # Links
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        rel = a.get("rel", [])
        links.append({
            "href": href,
            "text": text[:100],
            "rel": " ".join(rel) if isinstance(rel, list) else str(rel),
            "is_nofollow": "nofollow" in rel if isinstance(rel, list) else "nofollow" in str(rel),
        })

    # Images
    images = []
    for img in soup.find_all("img"):
        images.append({
            "src": img.get("src", ""),
            "alt": img.get("alt", ""),
            "width": img.get("width", ""),
            "height": img.get("height", ""),
            "has_alt": bool(img.get("alt", "").strip()),
        })

    # Schema/JSON-LD
    schemas = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            schemas.append(json.loads(script.string))
        except Exception:
            schemas.append({"raw": script.string[:500] if script.string else ""})

    # Text content
    text = _extract_text(fetched["html"])
    text_tokens = _tokenize(text)

    return {
        "url": fetched.get("url", url),
        "status": fetched.get("status", 0),
        "html": fetched.get("html", ""),
        "title": title,
        "meta_description": meta_desc,
        "meta_keywords": meta_keywords,
        "canonical": canonical,
        "robots": robots,
        "og_title": og_title,
        "og_description": og_desc,
        "og_image": og_image,
        "og_type": og_type,
        "headings": headings,
        "links": links,
        "images": images,
        "schemas": schemas,
        "text": text,
        "text_tokens": text_tokens,
        "word_count": len(text.split()),
        "soup": soup,
    }


# ═══════════════════════════════════════════════════════════════
# MAIN ANALYZER CLASS
# ═══════════════════════════════════════════════════════════════

class SEOAnalyzer:
    """All-in-one SEO analysis toolkit."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })

    # ── 1. KEYWORD RESEARCH ──────────────────────────────────

    def keyword_research(self, text: str, top_n: int = 20) -> dict:
        """Extract keywords from text using TF analysis."""
        tokens = _tokenize(text)
        if not tokens:
            return {"keywords": [], "bigrams": [], "trigrams": [], "error": "No meaningful text found"}

        # Single word frequency (TF)
        freq = Counter(tokens)
        total = len(tokens)
        keywords = [
            {"term": word, "count": count, "density": round(count / total * 100, 2)}
            for word, count in freq.most_common(top_n)
        ]

        # Bigrams
        bigrams = Counter()
        for i in range(len(tokens) - 1):
            bigrams[f"{tokens[i]} {tokens[i+1]}"] += 1
        top_bigrams = [
            {"term": term, "count": count, "density": round(count / total * 100, 2)}
            for term, count in bigrams.most_common(top_n // 2)
        ]

        # Trigrams
        trigrams = Counter()
        for i in range(len(tokens) - 2):
            trigrams[f"{tokens[i]} {tokens[i+1]} {tokens[i+2]}"] += 1
        top_trigrams = [
            {"term": term, "count": count, "density": round(count / total * 100, 2)}
            for term, count in trigrams.most_common(top_n // 3)
        ]

        return {
            "keywords": keywords,
            "bigrams": top_bigrams,
            "trigrams": top_trigrams,
            "total_words": total,
            "unique_words": len(freq),
            "vocabulary_richness": round(len(freq) / total * 100, 2),
        }

    # ── 2. CONTENT ANALYZER ──────────────────────────────────

    def content_analyze(self, url_or_html: str, target_keyword: str = "",
                        use_renderer: bool = True) -> dict:
        """Analyze content for SEO quality. Returns score + recommendations.

        If the fetched HTML is a JS-rendered SPA shell or a Cloudflare/bot-challenge
        interstitial (no real static content), returns `rendering_blocked=True` with an
        explanation instead of a misleading low score. When `use_renderer` is True (default)
        and a JS-capable renderer (Playwright) is available, it re-fetches the fully
        rendered DOM and analyses that instead.
        """
        data = _get_page_data(url_or_html)
        if "error" in data:
            return data

        # Detect pages whose static HTML isn't the real content (SPA / challenge).
        block = _detect_rendering_block(data)
        if block:
            # Try a JS-capable renderer before giving up.
            if use_renderer and _PLAYWRIGHT_OK and url_or_html.startswith("http"):
                rendered = _render_page(url_or_html)
                if "error" not in rendered and rendered.get("html"):
                    redata = _get_page_data(url_or_html, html=rendered["html"])
                    if "error" not in redata and not _detect_rendering_block(redata):
                        # Rendered DOM has real content — analyse it.
                        data = redata
                    else:
                        return {
                            "url": url_or_html,
                            "rendering_blocked": True,
                            "reason": block["reason"],
                            "detail": (block["detail"] +
                                       " A JavaScript renderer was attempted but the rendered "
                                       "DOM still contained no analysable content (the challenge "
                                       "may require solving a CAPTCHA)."),
                            "score": None,
                            "grade": "N/A",
                            "word_count": redata.get("word_count", 0) if "error" not in redata else 0,
                            "title": redata.get("title", "") if "error" not in redata else "",
                            "meta_description": redata.get("meta_description", "") if "error" not in redata else "",
                            "recommendations": [
                                {"priority": "high", "text": block["detail"]},
                            ],
                        }
                else:
                    return {
                        "url": url_or_html,
                        "rendering_blocked": True,
                        "reason": block["reason"],
                        "detail": block["detail"] + " Renderer error: " + rendered.get("error", "unknown"),
                        "score": None,
                        "grade": "N/A",
                        "word_count": 0,
                        "title": "",
                        "meta_description": "",
                        "recommendations": [{"priority": "high", "text": block["detail"]}],
                    }
            else:
                return {
                    "url": data.get("url", url_or_html),
                    "rendering_blocked": True,
                    "reason": block["reason"],
                    "detail": block["detail"],
                    "score": None,
                    "grade": "N/A",
                    "word_count": data.get("word_count", 0),
                    "title": data.get("title", ""),
                    "meta_description": data.get("meta_description", ""),
                    "recommendations": [
                        {
                            "priority": "high",
                            "text": (
                                "Static HTML analysis blocked: the page content is rendered "
                                "client-side (JavaScript) or gated behind a Cloudflare challenge. "
                                "Re-run with a JavaScript-capable renderer (headless Chrome / "
                                "Playwright) to analyse the real SEO signals (title, meta, headings, body)."
                            ),
                        }
                    ],
                }

        text = data["text"]
        tokens = data["text_tokens"]
        word_count = data["word_count"]
        title = data["title"]
        meta_desc = data["meta_description"]
        headings = data["headings"]

        # Readability
        readability = _readability_score(text)

        # Keyword density (if target provided)
        keyword_analysis = {}
        if target_keyword:
            target_lower = target_keyword.lower()
            target_count = text.lower().count(target_lower)
            keyword_analysis = {
                "target_keyword": target_keyword,
                "count": target_count,
                "density": round(target_count / max(word_count, 1) * 100, 2),
                "in_title": target_lower in title.lower(),
                "in_meta_description": target_lower in meta_desc.lower(),
                "in_h1": any(target_lower in h.lower() for h in headings.get("h1", [])),
                "in_h2": any(target_lower in h.lower() for h in headings.get("h2", [])),
            }

        # Top keywords
        top_keywords = self.keyword_research(text, top_n=15)

        # Content structure analysis
        structure = {
            "has_h1": len(headings.get("h1", [])) > 0,
            "h1_count": len(headings.get("h1", [])),
            "h1_text": headings.get("h1", []),
            "h2_count": len(headings.get("h2", [])),
            "h3_count": len(headings.get("h3", [])),
            "has_title": bool(title),
            "title_length": len(title),
            "has_meta_description": bool(meta_desc),
            "meta_description_length": len(meta_desc),
            "has_canonical": bool(data.get("canonical")),
            "has_og_tags": bool(data.get("og_title")),
            "has_schema": len(data.get("schemas", [])) > 0,
            "schema_count": len(data.get("schemas", [])),
        }

        # Image analysis
        images = data.get("images", [])
        images_without_alt = [img for img in images if not img["has_alt"]]
        image_analysis = {
            "total": len(images),
            "with_alt": sum(1 for img in images if img["has_alt"]),
            "without_alt": len(images_without_alt),
            "alt_coverage": round(sum(1 for img in images if img["has_alt"]) / max(len(images), 1) * 100, 1),
        }

        # Link analysis
        links = data.get("links", [])
        base_domain = urlparse(data.get("url", "")).netloc
        internal_links = [l for l in links if not l["href"].startswith("http") or urlparse(l["href"]).netloc == base_domain]
        external_links = [l for l in links if l["href"].startswith("http") and urlparse(l["href"]).netloc != base_domain]
        nofollow_links = [l for l in links if l["is_nofollow"]]

        link_analysis = {
            "total": len(links),
            "internal": len(internal_links),
            "external": len(external_links),
            "nofollow": len(nofollow_links),
            "dofollow": len(links) - len(nofollow_links),
        }

        # ── SEO Scoring (0-100) ──
        score = 0
        recommendations = []

        # Word count (15 points)
        if word_count >= 1500:
            score += 15
        elif word_count >= 800:
            score += 10
            recommendations.append({"priority": "medium", "text": f"Content is {word_count} words. Aim for 1500+ for better rankings."})
        elif word_count >= 300:
            score += 5
            recommendations.append({"priority": "high", "text": f"Content is only {word_count} words. Aim for 800+ minimum."})
        else:
            recommendations.append({"priority": "high", "text": f"Content is only {word_count} words. This is too thin for SEO."})

        # Title (15 points)
        if title:
            if 30 <= len(title) <= 60:
                score += 15
            elif len(title) < 30:
                score += 8
                recommendations.append({"priority": "medium", "text": f"Title is {len(title)} chars. Aim for 30-60 characters."})
            else:
                score += 8
                recommendations.append({"priority": "medium", "text": f"Title is {len(title)} chars. May be truncated in SERPs. Aim for 30-60."})
        else:
            recommendations.append({"priority": "high", "text": "Missing page title. This is critical for SEO."})

        # Meta description (10 points)
        if meta_desc:
            if 120 <= len(meta_desc) <= 160:
                score += 10
            else:
                score += 5
                recommendations.append({"priority": "medium", "text": f"Meta description is {len(meta_desc)} chars. Aim for 120-160."})
        else:
            recommendations.append({"priority": "high", "text": "Missing meta description. Add one for better CTR."})

        # Headings structure (15 points)
        if structure["has_h1"]:
            score += 5
            if structure["h1_count"] == 1:
                score += 5
            else:
                recommendations.append({"priority": "medium", "text": f"Found {structure['h1_count']} H1 tags. Use exactly one per page."})
        else:
            recommendations.append({"priority": "high", "text": "Missing H1 heading. Every page needs one."})

        if structure["h2_count"] >= 2:
            score += 5
        elif structure["h2_count"] == 1:
            score += 2
        else:
            recommendations.append({"priority": "medium", "text": "No H2 subheadings. Use them to structure content."})

        # Images (10 points)
        if image_analysis["total"] > 0:
            alt_pct = image_analysis["alt_coverage"]
            if alt_pct >= 90:
                score += 10
            elif alt_pct >= 50:
                score += 5
                recommendations.append({"priority": "medium", "text": f"{image_analysis['without_alt']} images missing alt text. Add descriptive alt tags."})
            else:
                score += 2
                recommendations.append({"priority": "high", "text": f"{image_analysis['without_alt']}/{image_analysis['total']} images missing alt text."})
        else:
            score += 5  # No images is neutral
            recommendations.append({"priority": "low", "text": "No images found. Add relevant images for engagement."})

        # Links (10 points)
        if link_analysis["total"] >= 3:
            score += 5
        if link_analysis["external"] >= 1:
            score += 3
        if link_analysis["internal"] >= 2:
            score += 2
        if link_analysis["total"] == 0:
            recommendations.append({"priority": "medium", "text": "No links found. Add internal and external links."})

        # Technical (15 points)
        if structure["has_canonical"]:
            score += 3
        else:
            recommendations.append({"priority": "low", "text": "No canonical tag. Add one to prevent duplicate content."})

        if structure["has_og_tags"]:
            score += 3
        else:
            recommendations.append({"priority": "low", "text": "No Open Graph tags. Add them for social sharing."})

        if structure["has_schema"]:
            score += 4
        else:
            recommendations.append({"priority": "medium", "text": "No schema markup. Add JSON-LD for rich snippets."})

        if data.get("robots", "") == "" or "noindex" not in data.get("robots", "").lower():
            score += 5
        else:
            score -= 10
            recommendations.append({"priority": "high", "text": "Page has noindex directive. It won't rank!"})

        # Readability (10 points)
        if readability["flesch_reading_ease"] >= 40:
            score += 10
        elif readability["flesch_reading_ease"] >= 20:
            score += 5
            recommendations.append({"priority": "medium", "text": f"Readability score is {readability['flesch_reading_ease']}. Simplify language for broader reach."})
        else:
            recommendations.append({"priority": "medium", "text": "Content is very difficult to read. Simplify sentences."})

        # Keyword targeting bonus
        if keyword_analysis:
            if keyword_analysis.get("in_title"):
                score += 3
            if keyword_analysis.get("in_h1"):
                score += 2
            if 0.5 <= keyword_analysis.get("density", 0) <= 2.5:
                score += 3
            elif keyword_analysis.get("density", 0) > 2.5:
                recommendations.append({"priority": "medium", "text": f"Keyword density is {keyword_analysis['density']}%. Risk of keyword stuffing. Keep under 2.5%."})

        score = max(0, min(100, score))

        # Grade
        if score >= 90:
            grade = "A+"
        elif score >= 80:
            grade = "A"
        elif score >= 70:
            grade = "B"
        elif score >= 60:
            grade = "C"
        elif score >= 50:
            grade = "D"
        else:
            grade = "F"

        return {
            "url": data.get("url", ""),
            "score": score,
            "grade": grade,
            "word_count": word_count,
            "title": title,
            "meta_description": meta_desc,
            "readability": readability,
            "structure": structure,
            "keyword_analysis": keyword_analysis,
            "top_keywords": top_keywords["keywords"][:10],
            "images": image_analysis,
            "links": link_analysis,
            "recommendations": sorted(recommendations, key=lambda r: {"high": 0, "medium": 1, "low": 2}.get(r["priority"], 3)),
        }

    # ── 3. SITE CRAWLER ──────────────────────────────────────

    def site_crawl(self, start_url: str, max_pages: int = 50) -> dict:
        """Crawl a site and extract SEO data for each page."""
        base_domain = urlparse(start_url).netloc
        if not base_domain:
            return {"error": "Invalid URL"}

        visited = set()
        to_visit = [start_url]
        pages = []
        broken_links = []
        redirect_chains = []

        while to_visit and len(visited) < max_pages:
            url = to_visit.pop(0)
            normalized = url.split("#")[0].rstrip("/")
            if normalized in visited:
                continue
            visited.add(normalized)

            fetched = _fetch_url(url)
            if "error" in fetched:
                broken_links.append({"url": url, "error": fetched["error"]})
                continue

            soup = fetched["soup"]
            title = soup.title.string.strip() if soup.title and soup.title.string else ""
            meta_desc = ""
            md = soup.find("meta", attrs={"name": "description"})
            if md:
                meta_desc = md.get("content", "")

            text = _extract_text(fetched["html"])
            word_count = len(text.split())

            # Check for redirects
            if fetched["url"] != url:
                redirect_chains.append({"from": url, "to": fetched["url"]})

            page_data = {
                "url": fetched["url"],
                "title": title,
                "meta_description": meta_desc,
                "word_count": word_count,
                "status": fetched["status"],
                "load_time": round(fetched.get("elapsed", 0), 2),
                "h1_count": len(soup.find_all("h1")),
                "h2_count": len(soup.find_all("h2")),
                "image_count": len(soup.find_all("img")),
                "images_without_alt": sum(1 for img in soup.find_all("img") if not img.get("alt", "").strip()),
                "internal_links": 0,
                "external_links": 0,
            }

            # Extract links for further crawling
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
                    continue
                full_url = urljoin(fetched["href"], href)
                parsed = urlparse(full_url)
                clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))

                if parsed.netloc == base_domain:
                    page_data["internal_links"] += 1
                    if clean_url not in visited and clean_url not in to_visit:
                        to_visit.append(clean_url)
                else:
                    page_data["external_links"] += 1

            pages.append(page_data)

        # Summary stats
        total_words = sum(p["word_count"] for p in pages)
        pages_without_title = [p for p in pages if not p["title"]]
        pages_without_meta = [p for p in pages if not p["meta_description"]]
        thin_pages = [p for p in pages if p["word_count"] < 300]
        slow_pages = [p for p in pages if p.get("load_time", 0) > 3]

        return {
            "start_url": start_url,
            "pages_crawled": len(pages),
            "pages": pages,
            "broken_links": broken_links,
            "redirect_chains": redirect_chains,
            "summary": {
                "total_words": total_words,
                "avg_words_per_page": round(total_words / max(len(pages), 1), 0),
                "pages_without_title": len(pages_without_title),
                "pages_without_meta": len(pages_without_meta),
                "thin_pages": len(thin_pages),
                "slow_pages": len(slow_pages),
                "total_broken_links": len(broken_links),
            },
        }

    # ── 4. COMPETITOR ANALYSIS ───────────────────────────────

    def competitor_compare(self, your_url: str, *competitor_urls: str) -> dict:
        """Compare your page against competitor pages."""
        all_urls = [your_url] + list(competitor_urls)
        results = []

        for url in all_urls:
            data = _get_page_data(url)
            if "error" in data:
                results.append({"url": url, "error": data["error"]})
                continue

            text = data["text"]
            tokens = data["text_tokens"]
            readability = _readability_score(text)
            keywords = self.keyword_research(text, top_n=10)

            links = data.get("links", [])
            base_domain = urlparse(data.get("url", "")).netloc
            internal = sum(1 for l in links if not l["href"].startswith("http") or urlparse(l["href"]).netloc == base_domain)
            external = len(links) - internal

            images = data.get("images", [])
            images_with_alt = sum(1 for img in images if img["has_alt"])

            results.append({
                "url": data.get("url", url),
                "domain": base_domain,
                "title": data["title"],
                "title_length": len(data["title"]),
                "meta_description": data["meta_description"],
                "meta_description_length": len(data["meta_description"]),
                "word_count": data["word_count"],
                "readability": readability["flesch_reading_ease"],
                "h1_count": len(data["headings"].get("h1", [])),
                "h2_count": len(data["headings"].get("h2", [])),
                "h3_count": len(data["headings"].get("h3", [])),
                "total_headings": sum(len(v) for v in data["headings"].values()),
                "internal_links": internal,
                "external_links": external,
                "total_links": len(links),
                "images": len(images),
                "images_with_alt": images_with_alt,
                "has_schema": len(data.get("schemas", [])) > 0,
                "has_canonical": bool(data.get("canonical")),
                "has_og_tags": bool(data.get("og_title")),
                "top_keywords": [k["term"] for k in keywords["keywords"][:5]],
                "keyword_density_top5": {k["term"]: k["density"] for k in keywords["keywords"][:5]},
                "unique_words": len(set(tokens)),
                "vocabulary_richness": round(len(set(tokens)) / max(len(tokens), 1) * 100, 2),
            })

        # Generate comparison insights
        if len(results) >= 2 and "error" not in results[0]:
            your = results[0]
            competitors = results[1:]
            insights = []

            avg_comp_words = sum(c.get("word_count", 0) for c in competitors if "error" not in c) / max(len(competitors), 1)
            if your["word_count"] < avg_comp_words * 0.8:
                insights.append(f"Your content ({your['word_count']} words) is shorter than competitors (avg {avg_comp_words:.0f} words). Consider expanding.")

            avg_comp_readability = sum(c.get("readability", 0) for c in competitors if "error" not in c) / max(len(competitors), 1)
            if your["readability"] < avg_comp_readability - 10:
                insights.append(f"Your readability ({your['readability']:.0f}) is lower than competitors (avg {avg_comp_readability:.0f}). Simplify language.")

            if not your["has_schema"]:
                has_schema_comp = any(c.get("has_schema") for c in competitors if "error" not in c)
                if has_schema_comp:
                    insights.append("Competitors use schema markup but you don't. Add JSON-LD for rich snippets.")

            if not your["has_og_tags"]:
                insights.append("Missing Open Graph tags. Competitors likely use them for social sharing.")

            if your["h1_count"] != 1:
                insights.append(f"You have {your['h1_count']} H1 tags. Best practice is exactly 1.")

            if your["images"] > 0 and your["images_with_alt"] < your["images"]:
                missing = your["images"] - your["images_with_alt"]
                insights.append(f"{missing} images missing alt text. Competitors likely have better alt coverage.")

            # Find keyword gaps
            your_kws = set(your.get("top_keywords", []))
            for comp in competitors:
                if "error" in comp:
                    continue
                comp_kws = set(comp.get("top_keywords", []))
                unique_to_comp = comp_kws - your_kws
                if unique_to_comp:
                    insights.append(f"Competitor ranks for keywords you don't mention: {', '.join(list(unique_to_comp)[:5])}")
        else:
            insights = []

        return {
            "your_page": results[0] if results else {},
            "competitors": results[1:],
            "insights": insights,
            "compared_at": datetime.now().isoformat(),
        }

    # ── 5. SCHEMA GENERATOR ──────────────────────────────────

    def schema_generate(self, schema_type: str, data: dict) -> dict:
        """Generate JSON-LD schema markup."""
        schemas = {
            "article": self._schema_article,
            "product": self._schema_product,
            "faq": self._schema_faq,
            "howto": self._schema_howto,
            "localbusiness": self._schema_localbusiness,
            "person": self._schema_person,
            "organization": self._schema_organization,
            "breadcrumb": self._schema_breadcrumb,
            "website": self._schema_website,
            "video": self._schema_video,
        }

        generator = schemas.get(schema_type.lower())
        if not generator:
            return {
                "error": f"Unknown schema type: {schema_type}",
                "available_types": list(schemas.keys()),
            }

        schema = generator(data)
        if "error" in schema:
            return schema

        # Validate
        validation = self._validate_schema(schema)

        return {
            "type": schema_type,
            "schema": schema,
            "json_ld": json.dumps(schema, indent=2),
            "html_snippet": f'<script type="application/ld+json">\n{json.dumps(schema, indent=2)}\n</script>',
            "validation": validation,
        }

    def _schema_article(self, data: dict) -> dict:
        required = ["headline", "author"]
        missing = [f for f in required if not data.get(f)]
        if missing:
            return {"error": f"Missing required fields: {missing}"}
        return {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": data["headline"],
            "author": {"@type": "Person", "name": data["author"]},
            "datePublished": data.get("datePublished", datetime.now().strftime("%Y-%m-%d")),
            "dateModified": data.get("dateModified", datetime.now().strftime("%Y-%m-%d")),
            "description": data.get("description", ""),
            "image": data.get("image", ""),
            "publisher": {"@type": "Organization", "name": data.get("publisher", ""), "logo": {"@type": "ImageObject", "url": data.get("publisherLogo", "")}},
            "mainEntityOfPage": {"@type": "WebPage", "@id": data.get("url", "")},
        }

    def _schema_product(self, data: dict) -> dict:
        if not data.get("name"):
            return {"error": "Missing required field: name"}
        schema = {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": data["name"],
            "description": data.get("description", ""),
            "image": data.get("image", ""),
            "brand": {"@type": "Brand", "name": data.get("brand", "")},
        }
        if data.get("price"):
            schema["offers"] = {
                "@type": "Offer",
                "price": str(data["price"]),
                "priceCurrency": data.get("currency", "USD"),
                "availability": f"https://schema.org/{data.get('availability', 'InStock')}",
            }
        if data.get("rating"):
            schema["aggregateRating"] = {
                "@type": "AggregateRating",
                "ratingValue": str(data["rating"]),
                "reviewCount": str(data.get("reviewCount", "1")),
            }
        return schema

    def _schema_faq(self, data: dict) -> dict:
        if not data.get("questions"):
            return {"error": "Missing required field: questions (list of {question, answer})"}
        return {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": q["question"],
                    "acceptedAnswer": {"@type": "Answer", "text": q["answer"]},
                }
                for q in data["questions"]
            ],
        }

    def _schema_howto(self, data: dict) -> dict:
        if not data.get("name") or not data.get("steps"):
            return {"error": "Missing required fields: name, steps"}
        return {
            "@context": "https://schema.org",
            "@type": "HowTo",
            "name": data["name"],
            "description": data.get("description", ""),
            "image": data.get("image", ""),
            "totalTime": data.get("totalTime", ""),
            "estimatedCost": {"@type": "MonetaryAmount", "currency": data.get("currency", "USD"), "value": data.get("cost", "0")},
            "step": [
                {
                    "@type": "HowToStep",
                    "name": step.get("name", f"Step {i+1}"),
                    "text": step.get("text", ""),
                    "image": step.get("image", ""),
                }
                for i, step in enumerate(data["steps"])
            ],
        }

    def _schema_localbusiness(self, data: dict) -> dict:
        if not data.get("name"):
            return {"error": "Missing required field: name"}
        schema = {
            "@context": "https://schema.org",
            "@type": data.get("type", "LocalBusiness"),
            "name": data["name"],
            "description": data.get("description", ""),
            "url": data.get("url", ""),
            "telephone": data.get("telephone", ""),
            "address": {
                "@type": "PostalAddress",
                "streetAddress": data.get("street", ""),
                "addressLocality": data.get("city", ""),
                "addressRegion": data.get("state", ""),
                "postalCode": data.get("postalCode", ""),
                "addressCountry": data.get("country", "AU"),
            },
        }
        if data.get("latitude") and data.get("longitude"):
            schema["geo"] = {
                "@type": "GeoCoordinates",
                "latitude": str(data["latitude"]),
                "longitude": str(data["longitude"]),
            }
        if data.get("openingHours"):
            schema["openingHours"] = data["openingHours"]
        if data.get("priceRange"):
            schema["priceRange"] = data["priceRange"]
        return schema

    def _schema_person(self, data: dict) -> dict:
        if not data.get("name"):
            return {"error": "Missing required field: name"}
        schema = {
            "@context": "https://schema.org",
            "@type": "Person",
            "name": data["name"],
            "url": data.get("url", ""),
            "jobTitle": data.get("jobTitle", ""),
            "description": data.get("description", ""),
        }
        if data.get("worksFor"):
            schema["worksFor"] = {"@type": "Organization", "name": data["worksFor"]}
        if data.get("sameAs"):
            schema["sameAs"] = data["sameAs"] if isinstance(data["sameAs"], list) else [data["sameAs"]]
        return schema

    def _schema_organization(self, data: dict) -> dict:
        if not data.get("name"):
            return {"error": "Missing required field: name"}
        schema = {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": data["name"],
            "url": data.get("url", ""),
            "logo": data.get("logo", ""),
            "description": data.get("description", ""),
        }
        if data.get("address"):
            schema["address"] = data["address"]
        if data.get("telephone"):
            schema["telephone"] = data["telephone"]
        if data.get("sameAs"):
            schema["sameAs"] = data["sameAs"] if isinstance(data["sameAs"], list) else [data["sameAs"]]
        return schema

    def _schema_breadcrumb(self, data: dict) -> dict:
        if not data.get("items"):
            return {"error": "Missing required field: items (list of {name, url})"}
        return {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": i + 1,
                    "name": item["name"],
                    "item": item.get("url", ""),
                }
                for i, item in enumerate(data["items"])
            ],
        }

    def _schema_website(self, data: dict) -> dict:
        schema = {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": data.get("name", ""),
            "url": data.get("url", ""),
            "description": data.get("description", ""),
        }
        if data.get("searchAction"):
            schema["potentialAction"] = {
                "@type": "SearchAction",
                "target": {"@type": "EntryPoint", "urlTemplate": data["searchAction"]},
                "query-input": "required name=search_term_string",
            }
        return schema

    def _schema_video(self, data: dict) -> dict:
        if not data.get("name") or not data.get("embedUrl"):
            return {"error": "Missing required fields: name, embedUrl"}
        return {
            "@context": "https://schema.org",
            "@type": "VideoObject",
            "name": data["name"],
            "description": data.get("description", ""),
            "thumbnailUrl": data.get("thumbnailUrl", ""),
            "uploadDate": data.get("uploadDate", datetime.now().strftime("%Y-%m-%d")),
            "embedUrl": data["embedUrl"],
            "duration": data.get("duration", ""),
        }

    def _validate_schema(self, schema: dict) -> dict:
        """Basic schema validation."""
        issues = []
        if "@context" not in schema:
            issues.append("Missing @context")
        if "@type" not in schema:
            issues.append("Missing @type")
        return {"valid": len(issues) == 0, "issues": issues}

    # ── 6. BACKLINK CHECKER ──────────────────────────────────

    def backlink_check(self, url_or_html: str) -> dict:
        """Analyze all links on a page."""
        data = _get_page_data(url_or_html)
        if "error" in data:
            return data

        links = data.get("links", [])
        base_domain = urlparse(data.get("url", "")).netloc

        internal_links = []
        external_links = []
        nofollow_links = []
        dofollow_links = []
        anchor_texts = Counter()
        domain_counts = Counter()

        for link in links:
            href = link["href"]
            text = link["text"] or "(no text)"
            anchor_texts[text] += 1

            if not href.startswith("http"):
                internal_links.append(link)
            else:
                parsed = urlparse(href)
                domain = parsed.netloc
                domain_counts[domain] += 1

                if domain == base_domain:
                    internal_links.append(link)
                else:
                    external_links.append(link)

            if link["is_nofollow"]:
                nofollow_links.append(link)
            else:
                dofollow_links.append(link)

        # Anchor text analysis
        empty_anchors = [l for l in links if not l["text"].strip()]
        generic_anchors = [l for l in links if l["text"].lower() in ("click here", "read more", "here", "link", "more", "click")]

        return {
            "url": data.get("url", ""),
            "total_links": len(links),
            "internal_links": len(internal_links),
            "external_links": len(external_links),
            "nofollow_links": len(nofollow_links),
            "dofollow_links": len(dofollow_links),
            "unique_external_domains": len(domain_counts),
            "top_external_domains": [{"domain": d, "count": c} for d, c in domain_counts.most_common(10)],
            "anchor_texts": [{"text": t, "count": c} for t, c in anchor_texts.most_common(20)],
            "empty_anchors": len(empty_anchors),
            "generic_anchors": len(generic_anchors),
            "recommendations": self._backlink_recommendations(
                len(links), len(internal_links), len(external_links),
                len(empty_anchors), len(generic_anchors), len(nofollow_links)
            ),
        }

    def _backlink_recommendations(self, total, internal, external, empty, generic, nofollow) -> list:
        recs = []
        if total == 0:
            recs.append({"priority": "high", "text": "No links found. Add internal and external links."})
        if internal == 0:
            recs.append({"priority": "medium", "text": "No internal links. Link to other pages on your site."})
        if external == 0:
            recs.append({"priority": "low", "text": "No external links. Link to authoritative sources for credibility."})
        if empty > 0:
            recs.append({"priority": "medium", "text": f"{empty} links have no anchor text. Add descriptive text for accessibility and SEO."})
        if generic > 0:
            recs.append({"priority": "medium", "text": f"{generic} links use generic text ('click here', 'read more'). Use descriptive anchor text."})
        if total > 0 and nofollow / total > 0.5:
            recs.append({"priority": "low", "text": f"{nofollow}/{total} links are nofollow. This is fine for user-generated content."})
        return recs

    # ── 7. FULL SEO AUDIT ────────────────────────────────────

    def full_audit(self, url: str, use_renderer: bool = True) -> dict:
        """Run a comprehensive SEO audit combining all modules.

        `use_renderer=True` enables a headless-Chromium (Playwright) fallback so
        JS-rendered / Cloudflare-gated pages are analysed from their real DOM.
        """
        start_time = time.time()

        # Content analysis (with optional JS renderer for SPA / CF pages)
        content = self.content_analyze(url, use_renderer=use_renderer)
        if "error" in content:
            return content
        # Backlink check (works on whatever static HTML is present).
        backlinks = self.backlink_check(url)
        # If the page is a JS-rendered SPA / Cloudflare challenge, surface that
        # clearly instead of producing a misleading low-score audit.
        if content.get("rendering_blocked"):
            return {
                "url": url,
                "rendering_blocked": True,
                "reason": content.get("reason"),
                "detail": content.get("detail"),
                "score": None,
                "grade": "N/A",
                "summary": {
                    "word_count": content.get("word_count", 0),
                    "title": content.get("title", ""),
                    "meta_description_length": len(content.get("meta_description", "") or ""),
                },
                "top_keywords": [],
                "recommendations": content.get("recommendations", []),
                "recommendation_counts": {"high": 1, "medium": 0, "low": 0, "total": 1},
                "content_analysis": content,
                "backlink_analysis": backlinks,
                "audited_at": datetime.now().isoformat(),
            }

        # Keyword research from page content
        data = _get_page_data(url)
        keywords = self.keyword_research(data.get("text", ""), top_n=20) if "error" not in data else {"keywords": []}

        elapsed = round(time.time() - start_time, 2)

        # Compile all recommendations
        all_recs = []
        all_recs.extend(content.get("recommendations", []))
        all_recs.extend(backlinks.get("recommendations", []))

        # Deduplicate
        seen = set()
        unique_recs = []
        for rec in all_recs:
            key = rec["text"]
            if key not in seen:
                seen.add(key)
                unique_recs.append(rec)

        # Priority counts
        high = sum(1 for r in unique_recs if r["priority"] == "high")
        medium = sum(1 for r in unique_recs if r["priority"] == "medium")
        low = sum(1 for r in unique_recs if r["priority"] == "low")

        return {
            "url": url,
            "audit_time": elapsed,
            "score": content.get("score", 0),
            "grade": content.get("grade", "F"),
            "summary": {
                "word_count": content.get("word_count", 0),
                "title": content.get("title", ""),
                "title_length": content.get("structure", {}).get("title_length", 0),
                "meta_description_length": content.get("structure", {}).get("meta_description_length", 0),
                "readability": content.get("readability", {}).get("flesch_reading_ease", 0),
                "total_links": backlinks.get("total_links", 0),
                "internal_links": backlinks.get("internal_links", 0),
                "external_links": backlinks.get("external_links", 0),
                "images": content.get("images", {}).get("total", 0),
                "images_without_alt": content.get("images", {}).get("without_alt", 0),
                "has_schema": content.get("structure", {}).get("has_schema", False),
                "has_canonical": content.get("structure", {}).get("has_canonical", False),
                "has_og_tags": content.get("structure", {}).get("has_og_tags", False),
            },
            "top_keywords": keywords.get("keywords", [])[:10],
            "recommendations": sorted(unique_recs, key=lambda r: {"high": 0, "medium": 1, "low": 2}.get(r["priority"], 3)),
            "recommendation_counts": {"high": high, "medium": medium, "low": low, "total": len(unique_recs)},
            "content_analysis": content,
            "backlink_analysis": backlinks,
            "audited_at": datetime.now().isoformat(),
        }


# ═══════════════════════════════════════════════════════════════
# CLI / STANDALONE
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    seo = SEOAnalyzer()

    if len(sys.argv) < 2:
        print("Usage: python3 seo_toolkit.py <command> [args]")
        print("Commands: content, keywords, crawl, compare, schema, backlinks, audit")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "content" and len(sys.argv) > 2:
        result = seo.content_analyze(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "keywords" and len(sys.argv) > 2:
        text = " ".join(sys.argv[2:])
        result = seo.keyword_research(text)
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "crawl" and len(sys.argv) > 2:
        result = seo.site_crawl(sys.argv[2], max_pages=int(sys.argv[3]) if len(sys.argv) > 3 else 20)
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "compare" and len(sys.argv) > 3:
        result = seo.competitor_compare(sys.argv[2], *sys.argv[3:])
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "schema" and len(sys.argv) > 2:
        result = seo.schema_generate(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {})
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "backlinks" and len(sys.argv) > 2:
        result = seo.backlink_check(sys.argv[2])
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "audit" and len(sys.argv) > 2:
        result = seo.full_audit(sys.argv[2])
        print(json.dumps(result, indent=2, default=str))

    else:
        print(f"Unknown command or missing args: {cmd}")
        sys.exit(1)
