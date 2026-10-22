#!/usr/bin/env python3
"""The Kdenlive bridge -- the Windows (or remote) side of the MCP.

Run this on the machine that has Kdenlive installed::

    python bridge/kdenlive_bridge.py --port 8765

Then point the MCP at it with ``KDENLIVE_MCP_BRIDGE_URL=http://<host>:8765``.

The bridge is intentionally tiny and depends on nothing but the Python
standard library, so it runs anywhere Python does -- including a stock
Windows install.

What it offers:

``GET  /health``          is the bridge alive, and what does this machine have?
``GET  /projects``        list ``.kdenlive`` files in a directory
``POST /render``          render a project with the local ``melt``
``GET  /render/<id>``     poll a render
``POST /open``            open a project in the Kdenlive GUI
``POST /file/read``       read a file
``POST /file/write``      write a file
``POST /path/translate``  how this machine sees a path (WSL interop helper)

Security: the bridge is meant for a trusted local network or a loopback
connection. It has no authentication, so do not expose it to the internet.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def find_melt() -> str | None:
    """Locate the MLT renderer on this machine.

    ``KDENLIVE_MCP_MELT`` wins over everything else, so the bridge and the MCP
    server can be pointed at the same portable MLT build.
    """
    override = os.environ.get("KDENLIVE_MCP_MELT", "").strip()
    if override and Path(override).exists():
        return override
    for name in ("melt", "melt.exe"):
        found = shutil.which(name)
        if found:
            return found
    candidates = []
    if os.name == "nt":
        for drive in ("C:", "D:", "E:"):
            candidates.append(Path(f"{drive}/Program Files/Kdenlive/bin/melt.exe"))
    else:
        candidates += [Path("/usr/bin/melt"), Path("/usr/local/bin/melt")]
        candidates.append(
            Path("/Applications/Kdenlive.app/Contents/MacOS/melt")
        )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def find_kdenlive() -> str | None:
    """Locate the Kdenlive GUI on this machine."""
    for name in ("kdenlive", "kdenlive.exe"):
        found = shutil.which(name)
        if found:
            return found
    candidates = []
    if os.name == "nt":
        for drive in ("C:", "D:", "E:"):
            candidates.append(Path(f"{drive}/Program Files/Kdenlive/bin/kdenlive.exe"))
    else:
        candidates += [
            Path("/usr/bin/kdenlive"),
            Path("/usr/local/bin/kdenlive"),
            Path("/Applications/Kdenlive.app/Contents/MacOS/kdenlive"),
        ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def kdenlive_version() -> str | None:
    """The version of the installed Kdenlive, if it can be determined."""
    binary = find_kdenlive()
    if not binary:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr or "").strip()
    match = re.search(r"(?:Kdenlive\s+)?(\d+\.\d+(?:\.\d+)?)", output)
    return match.group(1) if match else (output.splitlines()[0][:80] if output else None)


def melt_environment() -> dict[str, str]:
    """Extra environment variables for the ``melt`` process.

    ``KDENLIVE_MCP_MELT_ENV`` holds ``KEY=VALUE`` pairs separated by ``;`` or
    newlines, for portable MLT builds that need ``MLT_REPOSITORY`` and
    friends to be pointed at their own directories.
    """
    raw = os.environ.get("KDENLIVE_MCP_MELT_ENV", "")
    result: dict[str, str] = {}
    for item in re.split(r"[;\n]", raw):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, value = item.partition("=")
        if key.strip():
            result[key.strip()] = value
    return result


def translate_path(path: str) -> str:
    """Translate a WSL path into the native path of this machine."""
    text = str(path)
    match = re.match(r"^/mnt/([a-zA-Z])(/.*)$", text)
    if match and os.name == "nt":
        return f"{match.group(1).upper()}:{match.group(2).replace('/', chr(92))}"
    return text


# --------------------------------------------------------------------------
# request handler
# --------------------------------------------------------------------------


class BridgeHandler(BaseHTTPRequestHandler):
    """Handle the bridge's small JSON API."""

    server_version = "KdenliveBridge/0.1"

    # -- authentication ----------------------------------------------------

    def _authorised(self) -> bool:
        """Check the bearer token, if the bridge was started with one."""
        expected = getattr(self.server, "token", "")  # type: ignore[attr-defined]
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    # -- plumbing ----------------------------------------------------------

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        if self.server.verbose:  # type: ignore[attr-defined]
            sys.stderr.write("[bridge] " + format % args + "\n")

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if not self._authorised():
            self._send(401, {"error": "unauthorised", "ok": False})
            return

        if route == "/health":
            self._send(
                200,
                {
                    "ok": True,
                    "platform": f"{platform.system()} {platform.release()}",
                    "python": sys.version.split()[0],
                    "kdenlive_version": kdenlive_version(),
                    "kdenlive_path": find_kdenlive(),
                    "melt_path": find_melt(),
                    "detail": "Kdenlive bridge is running",
                },
            )
            return

        if route == "/projects":
            directory = (query.get("dir") or ["."])[0]
            root = Path(translate_path(directory)).expanduser()
            projects = []
            if root.is_dir():
                for path in sorted(root.rglob("*.kdenlive")):
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    projects.append(
                        {
                            "path": str(path),
                            "name": path.name,
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        }
                    )
            self._send(200, {"projects": projects})
            return

        match = re.match(r"^/render/([A-Za-z0-9_-]+)$", route)
        if match:
            job_id = match.group(1)
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if job is None:
                self._send(404, {"error": f"No render job {job_id}"})
                return
            self._send(200, job)
            return

        self._send(404, {"error": f"Unknown route {route}"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        payload = self._read_json()

        if not self._authorised():
            self._send(401, {"error": "unauthorised", "ok": False})
            return

        if route == "/render":
            self._start_render(payload)
            return

        if route == "/open":
            project = translate_path(payload.get("project", ""))
            binary = find_kdenlive()
            if not binary:
                self._send(
                    503,
                    {"error": "Kdenlive was not found on this machine", "ok": False},
                )
                return
            if not Path(project).exists():
                self._send(404, {"error": f"Project not found: {project}", "ok": False})
                return
            try:
                subprocess.Popen(  # noqa: S603
                    [binary, project],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                self._send(500, {"error": f"Could not start Kdenlive: {exc}", "ok": False})
                return
            self._send(200, {"ok": True, "project": project, "launched": binary})
            return

        if route == "/file/read":
            path = Path(translate_path(payload.get("path", "")))
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                self._send(404, {"error": str(exc), "ok": False})
                return
            self._send(200, {"ok": True, "content": content})
            return

        if route == "/file/write":
            path = Path(translate_path(payload.get("path", "")))
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(payload.get("content", ""), encoding="utf-8")
            except OSError as exc:
                self._send(500, {"error": str(exc), "ok": False})
                return
            self._send(200, {"ok": True, "path": str(path)})
            return

        if route == "/path/translate":
            path = payload.get("path", "")
            self._send(
                200,
                {
                    "ok": True,
                    "path": translate_path(path),
                    "platform": platform.system(),
                },
            )
            return

        self._send(404, {"error": f"Unknown route {route}"})

    # -- rendering ---------------------------------------------------------

    def _start_render(self, payload: dict) -> None:
        melt = find_melt()
        if not melt:
            self._send(503, {"error": "melt was not found on this machine", "ok": False})
            return

        project = translate_path(payload.get("project", ""))
        output = translate_path(payload.get("output") or "")
        if not output:
            output = str(Path(project).with_suffix(".mp4"))

        project_path = Path(project)
        if not project_path.exists():
            self._send(404, {"error": f"Project not found: {project}", "ok": False})
            return

        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        argv = [
            melt,
            project,
            "-consumer",
            f"avformat:{output}",
            f"vcodec={payload.get('video_codec', 'libx264')}",
            f"acodec={payload.get('audio_codec', 'aac')}",
        ]
        if payload.get("crf") is not None:
            argv.append(f"crf={payload['crf']}")

        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "status": "running",
            "progress": 0.0,
            "project": project,
            "output": output,
            "started_at": time.time(),
            "log": [],
        }
        with JOBS_LOCK:
            JOBS[job_id] = job

        thread = threading.Thread(target=self._run_render, args=(job, argv), daemon=True)
        thread.start()
        self._send(200, job)

    def _run_render(self, job: dict, argv: list[str]) -> None:
        try:
            environment = os.environ.copy()
            environment.update(melt_environment())
            process = subprocess.Popen(  # noqa: S603
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=environment,
            )
        except OSError as exc:
            job["status"] = "failed"
            job["message"] = f"could not start melt: {exc}"
            job["finished_at"] = time.time()
            return

        assert process.stdout is not None
        for line in process.stdout:
            text = line.rstrip()
            if not text:
                continue
            job["log"].append(text)
            del job["log"][:-200]
            match = re.search(r"current frame:\s*(\d+).*?percentage:\s*(\d+)", text, re.IGNORECASE)
            if match:
                job["frame"] = int(match.group(1))
                job["progress"] = float(match.group(2))

        process.wait()
        job["returncode"] = process.returncode
        job["finished_at"] = time.time()
        if process.returncode == 0 and Path(job["output"]).exists():
            job["status"] = "finished"
            job["progress"] = 100.0
            job["message"] = "render finished"
        else:
            job["status"] = "failed"
            job["message"] = f"melt exited with code {process.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Kdenlive bridge for the Kdenlive MCP server")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port to listen on")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    parser.add_argument(
        "--token",
        default=os.environ.get("KDENLIVE_BRIDGE_TOKEN", ""),
        help="require this bearer token (or set KDENLIVE_BRIDGE_TOKEN)",
    )
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    server.verbose = args.verbose  # type: ignore[attr-defined]
    server.token = args.token.strip()  # type: ignore[attr-defined]

    melt = find_melt()
    kdenlive = find_kdenlive()
    print(f"Kdenlive bridge listening on http://{args.host}:{args.port}")
    print(f"  kdenlive: {kdenlive or 'not found'}")
    print(f"  melt:     {melt or 'not found'}")
    if args.token.strip():
        print("  auth:     bearer token required")
    else:
        print("  auth:     NONE -- pass --token or set KDENLIVE_BRIDGE_TOKEN")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
