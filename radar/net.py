"""HTTP fetching with a small in-memory cache."""
import gzip
import json
import threading
import time
import urllib.request

UA = "IntegrityRadarDemo/1.0"
_cache: dict = {}
_lock = threading.Lock()


def fetch(url: str, ttl: int = 60, as_json: bool = True):
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    data = json.loads(raw) if as_json else raw.decode("utf-8-sig", errors="replace")
    with _lock:
        _cache[url] = (now, data)
    return data
