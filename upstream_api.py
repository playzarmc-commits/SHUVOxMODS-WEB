"""Client for the url2src MAX API — the real engine behind every core.py
function from here on (real browser rendering, loading-screen detection,
JS-rendered sites, screenshots, single-file snapshots). All calls are
blocking; callers already run in their own thread (a Flask request thread,
or one of jobs.py's job-executor threads), matching the old safe_get()
design this replaces.
"""

import time

import requests

import config

BASE = str(getattr(config, "UPSTREAM_API_URL", "")).strip().rstrip("/")
KEY = str(getattr(config, "UPSTREAM_API_KEY", "")).strip()
CONNECT_TIMEOUT = 20
RETRY_STATUSES = (502, 503, 504)


class UpstreamError(requests.exceptions.RequestException):
    """Upstream rendering API failed. Subclasses RequestException on purpose:
    app.py's existing `except requests.exceptions.RequestException` handler
    already turns this into a clean 502 — no route changes needed."""


def configured() -> bool:
    return bool(BASE and KEY)


def _headers():
    return {"x-api-key": KEY}


def _request(method, path, *, params=None, json=None, stream=False, retries=3, read_timeout=90):
    if not configured():
        raise UpstreamError(
            "The rendering engine is not configured (set UPSTREAM_API_URL / "
            "UPSTREAM_API_KEY in config.py)."
        )
    last = None
    for attempt in range(retries):
        try:
            r = requests.request(
                method, BASE + path, headers=_headers(), params=params, json=json,
                stream=stream, timeout=(CONNECT_TIMEOUT, read_timeout),
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            last = UpstreamError(f"Rendering engine unreachable: {e.__class__.__name__}")
            time.sleep(3)
            continue
        if r.status_code in RETRY_STATUSES and attempt < retries - 1:
            r.close()
            last = UpstreamError(f"Rendering engine busy/starting (HTTP {r.status_code}).")
            time.sleep(4)
            continue
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or r.text[:200]
            except ValueError:
                msg = r.text[:200]
            r.close()
            raise UpstreamError(msg or f"HTTP {r.status_code}")
        return r
    raise last or UpstreamError("Rendering engine request failed.")


def fetch(url: str, **opts) -> dict:
    """Blocking single call to /api/fetch — the upstream server itself waits
    for the job and returns the full result in one response (html included
    only if you pass html=1)."""
    opts.setdefault("html", 0)
    max_wait = opts.get("max_wait", 30)
    r = _request("GET", "/api/fetch", params={"url": url, **opts}, read_timeout=max_wait + 90)
    return r.json()


def start_job(url: str, **opts) -> dict:
    return _request("POST", "/api/jobs", json={"url": url, **opts}).json()


def poll(job_id: str, since: int = 0, timeout_s: int = 300, on_event=None) -> dict:
    """Blocks until the job is done, returns its `result` dict. Every event
    seen along the way is handed to on_event(ev) — use it to translate
    upstream progress into your own progress_cb()."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = _request("GET", f"/api/jobs/{job_id}", params={"since": since}, read_timeout=20).json()
        events = st.get("events") or []
        since += len(events)
        if on_event:
            for ev in events:
                on_event(ev)
        if st["status"] == "done":
            return st["result"]
        if st["status"] == "error":
            raise UpstreamError(st.get("error") or "The rendering engine could not process this site.")
        time.sleep(1.2)
    raise UpstreamError("Timed out waiting for the rendering engine.")


def download_zip_to(job_id: str, out_path: str, max_bytes: int):
    r = _request("GET", f"/api/jobs/{job_id}/zip", stream=True, read_timeout=120)
    try:
        size = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise UpstreamError("Result exceeds the configured size limit.")
                f.write(chunk)
    finally:
        r.close()


def download_file(job_id: str, name: str, max_bytes: int = 5 * 1024 * 1024) -> bytes:
    """Pulls one file out of the finished job's zip, e.g. 'meta.json',
    'links.json', 'screenshot.jpg' — without downloading the whole zip."""
    r = _request("GET", f"/api/jobs/{job_id}/file/{name}", stream=True, read_timeout=30)
    try:
        buf = bytearray()
        for chunk in r.iter_content(65536):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise UpstreamError("File too large.")
        return bytes(buf)
    finally:
        r.close()
