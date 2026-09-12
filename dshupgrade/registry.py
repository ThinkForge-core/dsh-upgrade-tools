"""The npm registry and the marketplace index — via stdlib ``urllib`` with a disk cache.

``npm view`` is deliberately not used here: under the standard sandbox it fails
(``EROFS`` on ``~/.npm/_cacache``), while this script must work from a plain
terminal without DSH and without npm at all.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import cmp_to_key
from pathlib import Path

from . import semver
from .paths import cache_dir

REGISTRY = "https://registry.npmjs.org"
# The public catalog that the `dshmarket` plugin reads as well (lib/catalog-npm.js):
# the same index, so the entries here match what the marketplace shows.
MARKET_INDEX = "https://awesome-dsh-plugin.com/plugins.json"
DSH_PACKAGE = "@deepseek-ai/dsh"
TIMEOUT_SECONDS = 30
CACHE_TTL_SECONDS = 6 * 60 * 60  # packuments rarely change; core tags matter more — cached shorter
CORE_CACHE_TTL_SECONDS = 15 * 60


def _cache_path(key: str) -> Path:
    safe = urllib.parse.quote(key, safe="")
    return cache_dir() / f"{safe}.json"


def _read_cache(key: str, ttl: int):
    path = _cache_path(key)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > ttl:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_cache(key: str, payload) -> None:
    path = _cache_path(key)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
    except OSError:
        pass


def fetch_json(url: str, *, ttl: int = CACHE_TTL_SECONDS, offline: bool = False):
    """Fetch JSON with caching. ``offline=True`` means: cache only."""
    cached = _read_cache(url, ttl)
    if cached is not None:
        return cached
    if offline:
        stale = _read_cache(url, ttl=10**9)
        if stale is None:
            raise RuntimeError(f"no cache entry for {url}, but offline mode is on")
        return stale

    request = urllib.request.Request(url, headers={"accept": "application/json",
                                                   "user-agent": "dsh-upgrade-tools"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as error:
        stale = _read_cache(url, ttl=10**9)
        if stale is not None:
            return stale
        raise RuntimeError(f"failed to fetch {url}: {error}") from error
    _write_cache(url, payload)
    return payload


def packument(name: str, *, offline: bool = False) -> dict:
    """The full package document."""
    return fetch_json(f"{REGISTRY}/{urllib.parse.quote(name, safe='')}", offline=offline)


def dist_tags(name: str, *, offline: bool = False) -> dict:
    """The package dist-tags."""
    return packument(name, offline=offline).get("dist-tags", {}) or {}


def manifest(name: str, version: str | None = None, *, offline: bool = False) -> dict | None:
    """Manifest of a package version (latest by default)."""
    document = packument(name, offline=offline)
    versions = document.get("versions", {}) or {}
    if version is None:
        version = (document.get("dist-tags", {}) or {}).get("latest")
    if version is None:
        return None
    return versions.get(version)


def all_versions(name: str, *, offline: bool = False) -> list[str]:
    """All published versions of a package."""
    return list((packument(name, offline=offline).get("versions", {}) or {}).keys())


def core_versions(*, offline: bool = False) -> dict:
    """Versions and tags of the DSH core.

    Returns ``{"tags": {...}, "versions": [{"version","time"}], "modified": str|None}``.
    """
    document = fetch_json(f"{REGISTRY}/{urllib.parse.quote(DSH_PACKAGE, safe='')}",
                          ttl=CORE_CACHE_TTL_SECONDS, offline=offline)
    times = document.get("time", {}) or {}
    versions = [
        {"version": version, "time": (times.get(version) or "")[:10] or None}
        for version in (document.get("versions", {}) or {})
    ]
    versions.sort(key=lambda item: item["time"] or "")
    return {
        "tags": document.get("dist-tags", {}) or {},
        "versions": versions,
        "modified": times.get("modified"),
    }


def newest_core_version(*, offline: bool = False) -> str | None:
    """Newest published core version (highest semver, prereleases included).

    This is the automatic upgrade target. The ``latest`` dist-tag is NOT used for
    it: on the real registry ``latest`` was ``0.1.5-rc.1`` while ``0.1.5-rc.2``
    was already published under ``next``, so the tag points at an older release
    than the newest one.
    """
    try:
        versions = [item["version"] for item in core_versions(offline=offline)["versions"]]
    except RuntimeError:
        return None
    parsed = [version for version in versions if semver.parse_version(version) is not None]
    if not parsed:
        return None
    return max(parsed, key=cmp_to_key(semver.compare_version))


def market_index(*, offline: bool = False) -> dict:
    """The plugin marketplace index."""
    document = fetch_json(MARKET_INDEX, offline=offline)
    return {
        "updated": document.get("updated"),
        "count": document.get("count", 0),
        "plugins": document.get("plugins", []) or [],
    }


def market_entries(npm_name: str, *, offline: bool = False) -> list[dict]:
    """Marketplace entries for a specific npm package."""
    return [p for p in market_index(offline=offline)["plugins"] if p.get("npm") == npm_name]
