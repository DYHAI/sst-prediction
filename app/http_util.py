"""极简 HTTP 客户端：重试 + 超时 + 磁盘缓存。

只用标准库。国内到 NOAA/Cloudflare 的链路偶发超时，所以默认重试 3 次。
"""

from __future__ import annotations

import gzip
import hashlib
import os
import time
import urllib.error
import urllib.request

UA = "SST-Prediction/1.0 (+https://sst.playai.org.cn; data ingestion bot)"
CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache"
)


def _cache_path(key: str, ext: str) -> str:
    h = hashlib.sha256(key.encode()).hexdigest()[:24]
    d = os.path.join(CACHE_DIR, "http")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{h}{ext}")


def fetch(
    url: str,
    *,
    timeout: float = 60.0,
    retries: int = 3,
    cache: bool = False,
    cache_ext: str = ".bin",
    ttl: float | None = None,
    headers: dict | None = None,
) -> bytes:
    if cache:
        p = _cache_path(url, cache_ext)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            age = time.time() - os.path.getmtime(p)
            if ttl is None or age < ttl:
                with open(p, "rb") as f:
                    return f.read()

    hdrs = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                if cache:
                    with open(_cache_path(url, cache_ext), "wb") as f:
                        f.write(raw)
                return raw
        except urllib.error.HTTPError as e:
            # 404/400 是确定性的，不重试
            if e.code in (400, 404):
                raise
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed after {retries} attempts: {url}: {last_err}")


def fetch_text(url: str, **kw) -> str:
    return fetch(url, **kw).decode("utf-8", errors="replace")


def download_to(url: str, path: str, *, timeout: float = 180.0, retries: int = 3) -> str:
    raw = fetch(url, timeout=timeout, retries=retries)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)
    return path
