"""Talking to a Kdenlive running on another machine (or on Windows from WSL).

The MCP server edits ``.kdenlive`` files directly, so the *editing* half works
across a WSL boundary for free: a project living in ``C:\\Users\\me\\Videos``
is reachable from WSL as ``/mnt/c/Users/me/Videos``.

What does *not* cross the boundary on its own is the part that needs the
machine that actually runs Kdenlive: rendering with the local MLT build, and
opening the project in the running editor.  For that the MCP talks to a tiny
companion process -- the *bridge* -- over HTTP.

The bridge is deliberately boring: a stdlib-only HTTP server that can be
started on the Windows side with ``python bridge/kdenlive_bridge.py``.  The MCP
discovers it automatically:

* ``KDENLIVE_MCP_BRIDGE_URL`` if set (use this verbatim),
* ``http://127.0.0.1:<port>`` (works when Windows 11 mirrored networking is on),
* the Windows host address as seen from inside WSL, derived from
  ``/etc/resolv.conf`` or the default route.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .errors import BridgeError

DEFAULT_PORT = 8765
DEFAULT_URL = f"http://127.0.0.1:{DEFAULT_PORT}"

_RESOLV_NAMESERVER = re.compile(r"^\s*nameserver\s+(\S+)", re.MULTILINE)


@dataclass(slots=True)
class BridgeInfo:
    """What the bridge reported about the machine it runs on."""

    reachable: bool
    url: str = ""
    platform: str = ""
    kdenlive_version: str | None = None
    melt_path: str | None = None
    detail: str = ""

    def describe(self) -> dict[str, object]:
        return {
            "reachable": self.reachable,
            "url": self.url,
            "platform": self.platform,
            "kdenlive_version": self.kdenlive_version,
            "melt_path": self.melt_path,
            "detail": self.detail,
        }


def _resolv_conf_nameservers() -> list[str]:
    path = Path("/etc/resolv.conf")
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [match for match in _RESOLV_NAMESERVER.findall(text) if match != "127.0.0.1"]


def _default_gateway() -> str | None:
    """The default route gateway, which is the Windows host from inside WSL."""
    for command in (
        ["ip", "route", "show", "default"],
        ["route", "-n", "get", "default"],
    ):
        try:
            import subprocess

            result = subprocess.run(  # noqa: S603
                command, capture_output=True, text=True, timeout=5, check=False
            )
        except (OSError, Exception):  # noqa: B014 - subprocess may be missing entirely
            continue
        if result.returncode != 0:
            continue
        match = re.search(r"default\s+(?:via\s+)?(\d+\.\d+\.\d+\.\d+)", result.stdout)
        if match:
            return match.group(1)
        match = re.search(r"gateway:\s*(\d+\.\d+\.\d+\.\d+)", result.stdout)
        if match:
            return match.group(1)
    return None


def candidate_urls() -> list[str]:
    """URLs to try when looking for the bridge, most specific first."""
    explicit = os.environ.get("KDENLIVE_MCP_BRIDGE_URL")
    if explicit:
        return [explicit.rstrip("/")]

    urls = [DEFAULT_URL]

    # Inside WSL the Windows host is reachable through the nameserver entry
    # or the default gateway.
    try:
        import platform

        if "microsoft" in platform.uname().release.lower():
            for host in _resolv_conf_nameservers():
                urls.append(f"http://{host}:{DEFAULT_PORT}")
            gateway = _default_gateway()
            if gateway:
                urls.append(f"http://{gateway}:{DEFAULT_PORT}")
    except Exception:  # pragma: no cover - defensive
        pass

    # Preserve order but drop duplicates.
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


class BridgeClient:
    """A tiny HTTP client for the Kdenlive bridge.

    Set ``KDENLIVE_MCP_BRIDGE_TOKEN`` to the same value the bridge was started
    with to authenticate the requests.
    """

    def __init__(self, base_url: str | None = None, timeout: float = 15.0) -> None:
        self.base_url = (base_url or os.environ.get("KDENLIVE_MCP_BRIDGE_URL") or "").rstrip("/")
        self.timeout = timeout
        self.token = os.environ.get("KDENLIVE_MCP_BRIDGE_TOKEN", "").strip()
        self._resolved: str | None = None

    # -- low level ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        base: str | None = None,
    ) -> dict:
        url = f"{base or self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:  # noqa: BLE001
                pass
            raise BridgeError(f"Bridge returned HTTP {exc.code} for {url}: {detail}") from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise BridgeError(f"Could not reach the bridge at {url}: {exc}") from exc
        try:
            return json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            raise BridgeError(f"Bridge at {url} returned invalid JSON") from exc

    # -- discovery ---------------------------------------------------------

    def resolve(self) -> str:
        """Find a reachable bridge and remember its URL."""
        if self._resolved:
            return self._resolved
        candidates = [self.base_url] if self.base_url else candidate_urls()
        last_error: Exception | None = None
        for url in candidates:
            if not url:
                continue
            try:
                self._request("GET", "/health", base=url)
            except BridgeError as exc:
                last_error = exc
                continue
            self._resolved = url
            self.base_url = url
            return url
        hint = ""
        if self.token:
            hint = (
                " The bridge answered but rejected the token -- check that "
                "KDENLIVE_MCP_BRIDGE_TOKEN matches the one it was started with."
            )
        raise BridgeError(
            "No Kdenlive bridge found. Start one with `python bridge/kdenlive_bridge.py` on the "
            "machine running Kdenlive, or set KDENLIVE_MCP_BRIDGE_URL."
            + hint
            + f" Last error: {last_error}"
        )

    def health(self) -> BridgeInfo:
        """Check whether a bridge is reachable and what it can do."""
        try:
            url = self.resolve()
        except BridgeError as exc:
            return BridgeInfo(reachable=False, detail=str(exc))
        payload = self._request("GET", "/health", base=url)
        return BridgeInfo(
            reachable=bool(payload.get("ok", True)),
            url=url,
            platform=str(payload.get("platform", "")),
            kdenlive_version=payload.get("kdenlive_version"),
            melt_path=payload.get("melt_path"),
            detail=str(payload.get("detail", "")),
        )

    # -- operations --------------------------------------------------------

    def list_projects(self, directory: str) -> list[dict]:
        """List ``.kdenlive`` projects in a directory on the bridge machine."""
        url = self.resolve()
        query = urllib.parse.urlencode({"dir": directory})
        payload = self._request("GET", f"/projects?{query}", base=url)
        return list(payload.get("projects") or [])

    def render(self, project: str, output: str, **options: object) -> dict:
        """Start a render on the bridge machine."""
        url = self.resolve()
        payload = {"project": project, "output": output}
        payload.update(options)
        return self._request("POST", "/render", payload, base=url)

    def render_status(self, job_id: str) -> dict:
        url = self.resolve()
        return self._request("GET", f"/render/{job_id}", base=url)

    def open_in_kdenlive(self, project: str) -> dict:
        """Ask the bridge machine to open a project in the Kdenlive GUI."""
        url = self.resolve()
        return self._request("POST", "/open", {"project": project}, base=url)

    def read_file(self, path: str) -> str:
        url = self.resolve()
        payload = self._request("POST", "/file/read", {"path": path}, base=url)
        return str(payload.get("content", ""))

    def write_file(self, path: str, content: str) -> dict:
        url = self.resolve()
        return self._request("POST", "/file/write", {"path": path, "content": content}, base=url)

    def native_path(self, path: str) -> str:
        """Ask the bridge machine how it sees a path (useful from WSL)."""
        url = self.resolve()
        payload = self._request("POST", "/path/translate", {"path": path}, base=url)
        return str(payload.get("path", path))
