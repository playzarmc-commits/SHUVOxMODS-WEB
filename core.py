"""Engine: URL safety (unchanged) + the url2src MAX API for everything that
actually fetches a page — single-page HTML, SEO/tech analysis, offline ZIP
packaging, and full-site crawling. The old requests+BeautifulSoup scraper
is gone; a real browser (with loading-screen detection, JS-rendered sites,
screenshots) now does the work on the upstream service. Every public
function below keeps the exact return shape app.py / jobs.py already
expect, so nothing outside this file needed to change.
"""

import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlparse, urlunparse

import config
import upstream_api as upstream

UpstreamError = upstream.UpstreamError


class UnsafeURL(ValueError):
    """URL points to a local / non-public destination or is malformed."""


class ResponseTooLarge(ValueError):
    """Kept for compatibility with older callers; the upstream API enforces
    its own size limits now (surfaced as UpstreamError -> HTTP 502)."""


# ═══════════════════════ URL SAFETY (unchanged — pure local checks) ═══════════════════════
URL_PATTERN = re.compile(
    r"^https?://"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]{2,}"
    r"(?::\d{1,5})?"
    r"(?:/[^\s]*)?$"
)


def is_valid_url(text: str) -> bool:
    if not text:
        return False
    text = text.strip()
    if " " in text or "\n" in text or not URL_PATTERN.match(text):
        return False
    try:
        parsed = urlparse(text)
        return bool(parsed.scheme and parsed.netloc and "." in parsed.netloc)
    except Exception:
        return False


def _validated_public_url(url: str) -> str:
    """Allow only public HTTP(S) hosts; reject credentials and private IPs.
    This is defense-in-depth on top of the upstream API's own private-
    address guard — still worth doing here before a job is even queued."""
    try:
        parsed = urlparse(str(url).strip())
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https") or not parsed.hostname:
            raise UnsafeURL("Only public HTTP/HTTPS URLs are supported.")
        if parsed.username or parsed.password:
            raise UnsafeURL("URLs containing embedded credentials are not allowed.")
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        default_port = 443 if scheme == "https" else 80
        port = parsed.port or default_port
        if port != default_port:
            raise UnsafeURL("Only standard HTTP/HTTPS ports are supported.")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
            (".localhost", ".local", ".internal", ".lan")
        ):
            raise UnsafeURL("Local and private network hosts are not allowed.")
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = [
                ipaddress.ip_address(rec[4][0].split("%", 1)[0]) for rec in records
            ]
        if not addresses or any(not a.is_global for a in addresses):
            raise UnsafeURL("Only publicly routable hosts are allowed.")
        netloc = f"[{host}]" if ":" in host else host
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse((scheme, netloc, parsed.path or "/", parsed.params, parsed.query, ""))
    except UnsafeURL:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        raise UnsafeURL("That URL is invalid or could not be resolved safely.") from exc


def extract_title(html_content: str) -> str:
    title = "page"
    try:
        lower = html_content.lower()
        if "<title>" in lower:
            start_idx = lower.find("<title>") + 7
            end_idx = lower.find("</title>")
            raw = html_content[start_idx:end_idx].strip()
            cleaned = "".join(c for c in raw if c.isalnum() or c in (" ", "_", "-")).strip()[:40]
            if cleaned:
                title = cleaned
    except Exception:
        pass
    return title


_PHASE_MESSAGE = {
    "http_fetch": "Fetching page (fast mode, no browser needed)...",
    "scrolling": "Scrolling to load lazy content...",
    "collecting_assets": "Collecting CSS / JS / images / fonts...",
    "crawling": "Crawling internal pages...",
    "screenshot": "Taking a screenshot...",
    "packing": "Packing the ZIP...",
    "fallback_to_browser": "Page needs JavaScript — switching to a full browser...",
}


def _headings_from_meta(meta_headings: dict) -> dict:
    out = {f"h{n}": 0 for n in range(1, 7)}
    for k, v in (meta_headings or {}).items():
        if k in out:
            out[k] = len(v or [])
    return out


# ═══════════════════════ SINGLE-PAGE OPERATIONS ═══════════════════════
def fetch_html(url: str) -> dict:
    """Full rendered HTML of one page (JS-rendered sites included)."""
    data = upstream.fetch(url, engine="auto", speed="balanced", crawl=0, assets=0,
                          screenshot=0, snapshot=0, sourcemaps=0, html=1, max_wait=25)
    html = data.get("html") or ""
    return {
        "url": url,
        "final_url": data.get("final_url") or url,
        "status": data.get("status"),
        "title": extract_title(html),
        "size_bytes": data.get("html_length") or len(html.encode("utf-8")),
        "blocked": ("The site looks bot-protected or returned very little content."
                   if data.get("blocked") else ""),
        "html": html,
    }


def seo_report(url: str) -> dict:
    data = upstream.fetch(url, engine="auto", speed="turbo", crawl=0, assets=0,
                          screenshot=0, snapshot=0, sourcemaps=0, html=0, max_wait=20)
    job_id = data["job_id"]
    try:
        meta = json.loads(upstream.download_file(job_id, "meta.json"))
    except Exception:
        meta = {}
    try:
        links_data = json.loads(upstream.download_file(job_id, "links.json"))
    except Exception:
        links_data = {}
    m = meta.get("meta") or {}
    counts = meta.get("counts") or {}
    timings = data.get("timings") or {}
    return {
        "url": url,
        "status": data.get("status"),
        "load_time": round((timings.get("total_ms") or 0) / 1000, 2),
        "size_kb": round((data.get("html_length") or 0) / 1024, 2),
        "title": m.get("title") or data.get("title") or "Not found",
        "meta_description": m.get("description") or "Not found",
        "headings": _headings_from_meta(meta.get("headings")),
        "links": links_data.get("total", 0),
        "images": counts.get("images", 0),
    }


def tech_report(url: str) -> dict:
    data = upstream.fetch(url, engine="auto", speed="turbo", crawl=0, assets=0,
                          screenshot=0, snapshot=0, sourcemaps=0, html=0, max_wait=20)
    detected = data.get("tech") or []
    return {"url": url, "detected": detected or ["Could not confidently detect a known stack"]}


# ═══════════════════════ OFFLINE PACKAGING (now via the upstream API) ═══════════════════════
def build_offline_zip_from_url(url: str, out_path: str, progress_cb=None) -> dict:
    """Single page + every linked asset (CSS/JS/images/fonts), rendered in a
    real browser, written to out_path as a ZIP. Returns
    {"pages", "assets", "skipped"}."""
    job = upstream.start_job(url, engine="auto", speed="balanced", crawl=0, assets=1,
                             screenshot=0, snapshot=0, sourcemaps=0, max_wait=40)

    def on_event(ev):
        if not progress_cb:
            return
        t = ev.get("type")
        if t == "phase":
            progress_cb(pages=0, visited=0, message=_PHASE_MESSAGE.get(ev.get("phase"), "Working..."))
        elif t == "tick":
            progress_cb(pages=0, visited=0,
                       message=f"{ev.get('elapsed_s', '?')}s — {ev.get('resources_captured', 0)} files captured")
        elif t == "auto_unlock_click":
            progress_cb(pages=0, visited=0, message=f"Clicked through a gate: {ev.get('text', '')}")

    result = upstream.poll(job["job_id"], on_event=on_event, timeout_s=180)
    if progress_cb:
        progress_cb(pages=1, visited=1, message="Packing ZIP...")
    upstream.download_zip_to(job["job_id"], out_path, config.MAX_TOTAL_DOWNLOAD_BYTES)
    res = result.get("resources") or {}
    return {"pages": 1, "assets": res.get("count", 0), "skipped": res.get("skipped", 0)}


def crawl_full_site(start_url: str, out_path: str, progress_cb=None) -> dict:
    """Crawls every same-host page (bounded by config.MAX_SITE_PAGES /
    MAX_SITE_DEPTH), shares one asset pool, rewrites internal links, writes
    the ZIP to out_path. Returns {"pages", "assets", "skipped"}."""
    pages_seen = [0]

    def on_event(ev):
        if not progress_cb:
            return
        t = ev.get("type")
        if t == "page":
            pages_seen[0] += 1
            progress_cb(pages=pages_seen[0], visited=pages_seen[0], message=ev.get("url", ""))
        elif t == "phase":
            progress_cb(pages=pages_seen[0], visited=pages_seen[0],
                       message=_PHASE_MESSAGE.get(ev.get("phase"), "Working..."))
        elif t == "tick":
            progress_cb(pages=pages_seen[0], visited=pages_seen[0],
                       message=f"{ev.get('elapsed_s', '?')}s — {ev.get('resources_captured', 0)} files captured")

    max_pages = max(1, min(config.MAX_SITE_PAGES, 50))   # upstream's own per-job cap is 50
    job = upstream.start_job(
        start_url, engine="auto", speed="balanced", crawl=1, max_pages=max_pages,
        depth=min(config.MAX_SITE_DEPTH, 5), assets=1, screenshot=0, snapshot=0,
        sourcemaps=0, max_wait=45, crawl_budget=max(90, max_pages * 4),
    )
    result = upstream.poll(job["job_id"], on_event=on_event, timeout_s=max(180, max_pages * 5))
    if progress_cb:
        pages_now = len(result.get("pages") or []) or 1
        progress_cb(pages=pages_now, visited=pages_now, message="Packing ZIP...")
    upstream.download_zip_to(job["job_id"], out_path, config.MAX_TOTAL_DOWNLOAD_BYTES)
    res = result.get("resources") or {}
    pages = result.get("pages") or []
    return {"pages": len(pages) or 1, "assets": res.get("count", 0), "skipped": res.get("skipped", 0)}
