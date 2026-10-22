"""Rendering a project with MLT's ``melt`` command line renderer.

``melt`` ships with Kdenlive and renders a ``.kdenlive`` project directly --
MLT simply ignores the Kdenlive specific properties and renders the timeline.
That makes headless rendering possible with no GUI at all.

Renders run on a background thread so a long job never blocks the MCP
transport; the caller gets a job id back and polls :func:`render_status`.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .binaries import find_melt, is_wsl, to_windows_path
from .errors import RenderError

_PROGRESS_RE = re.compile(r"current frame:\s*(\d+).*?percentage:\s*(\d+)", re.IGNORECASE)
_FRAME_RE = re.compile(r"frame:\s*(\d+)", re.IGNORECASE)


def melt_environment() -> dict[str, str]:
    """Extra environment variables for the ``melt`` process.

    Portable MLT builds (AppImages, the Shotcut bundles, relocatable
    installs) need ``MLT_REPOSITORY``/``MLT_DATA``/``LD_LIBRARY_PATH`` to be
    pointed at their own directories.  Set ``KDENLIVE_MCP_MELT_ENV`` to a
    list of ``KEY=VALUE`` pairs separated by ``;`` or newlines to supply them.
    """
    raw = os.environ.get("KDENLIVE_MCP_MELT_ENV", "")
    result: dict[str, str] = {}
    for item in re.split(r"[;\n]", raw):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, value = item.partition("=")
        key = key.strip()
        if key:
            result[key] = value
    return result


@dataclass(slots=True)
class RenderRequest:
    """Everything needed to build a ``melt`` command line."""

    project: Path
    output: Path
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    crf: int | None = 23
    preset: str | None = None
    bitrate: str | None = None
    scale: str | None = None
    audio_bitrate: str | None = None
    threads: int | None = None
    start_frame: int | None = None
    end_frame: int | None = None
    extra_args: list[str] = field(default_factory=list)
    overwrite: bool = True

    def consumer_args(self, output: Path | None = None) -> list[str]:
        """The ``-consumer`` arguments for this request.

        ``output`` overrides the request's own path; the WSL interop needs
        that because a Windows ``melt`` cannot write to a ``/mnt/...`` path.
        """
        target = self.output if output is None else output
        args = [f"avformat:{target}"]
        if self.video_codec:
            args.append(f"vcodec={self.video_codec}")
        if self.audio_codec:
            args.append(f"acodec={self.audio_codec}")
        if self.crf is not None:
            args.append(f"crf={self.crf}")
        if self.preset:
            args.append(f"preset={self.preset}")
        if self.bitrate:
            args.append(f"bitrate={self.bitrate}")
        if self.scale:
            args.append(f"scale={self.scale}")
        if self.audio_bitrate:
            args.append(f"ab={self.audio_bitrate}")
        if self.threads:
            args.append(f"threads={self.threads}")
        args.append("real_time=-1")
        return args


@dataclass
class RenderJob:
    """State of a running render."""

    job_id: str
    request: RenderRequest
    status: str = "pending"  # pending | running | finished | failed | cancelled
    progress: float = 0.0
    frame: int = 0
    message: str = ""
    returncode: int | None = None
    log: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None

    def describe(self) -> dict[str, object]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress_percent": round(self.progress, 1),
            "frame": self.frame,
            "output": str(self.request.output),
            "project": str(self.request.project),
            "elapsed_seconds": round(elapsed, 1),
            "returncode": self.returncode,
            "message": self.message,
            "log_tail": self.log[-20:],
        }


class RenderManager:
    """Keeps track of render jobs started by this server."""

    def __init__(self) -> None:
        self._jobs: dict[str, RenderJob] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> RenderJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise RenderError(f"No render job with id {job_id!r}")
        return job

    def jobs(self) -> list[RenderJob]:
        with self._lock:
            return list(self._jobs.values())

    def start(self, request: RenderRequest) -> RenderJob:
        """Start a render on a background thread."""
        melt = find_melt()
        if not melt.available or not melt.path:
            raise RenderError(
                "melt (the MLT renderer) was not found. Install Kdenlive (it ships with melt), "
                "or set KDENLIVE_MCP_MELT to the path of the melt executable."
            )
        if not request.project.exists():
            raise RenderError(f"Project file not found: {request.project}")

        job = RenderJob(job_id=uuid.uuid4().hex[:12], request=request)
        with self._lock:
            self._jobs[job.job_id] = job

        thread = threading.Thread(target=self._run, args=(job, melt.path), daemon=True)
        job.thread = thread
        thread.start()
        return job

    def cancel(self, job_id: str) -> RenderJob:
        job = self.get(job_id)
        process = job.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            job.status = "cancelled"
            job.message = "cancelled by user"
            job.finished_at = time.time()
        return job

    def _run(self, job: RenderJob, melt_path: str) -> None:
        request = job.request
        argv = build_melt_command(melt_path, request)
        job.status = "running"
        job.message = "rendering"
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
            job.status = "failed"
            job.message = f"could not start melt: {exc}"
            job.finished_at = time.time()
            return

        job.process = process
        assert process.stdout is not None
        for line in process.stdout:
            text = line.rstrip()
            if text:
                job.log.append(text)
                del job.log[:-400]
                match = _PROGRESS_RE.search(text)
                if match:
                    job.frame = int(match.group(1))
                    job.progress = float(match.group(2))
                else:
                    frame_match = _FRAME_RE.search(text)
                    if frame_match:
                        job.frame = int(frame_match.group(1))

        process.wait()
        job.returncode = process.returncode
        job.finished_at = time.time()
        if job.status == "cancelled":
            return
        if process.returncode == 0 and request.output.exists():
            job.status = "finished"
            job.progress = 100.0
            job.message = "render finished"
        else:
            job.status = "failed"
            job.message = (
                f"melt exited with code {process.returncode}"
                + ("" if request.output.exists() else " and no output file was produced")
            )


def build_melt_command(melt_path: str, request: RenderRequest) -> list[str]:
    """Assemble the ``melt`` command line for a render request."""
    project = request.project
    output = request.output

    # When melt is a Windows binary reached through WSL interop it only
    # understands Windows paths.
    if melt_path.lower().endswith(".exe") and is_wsl():
        project = Path(to_windows_path(project))
        output = Path(to_windows_path(output))

    argv = [melt_path]
    if request.start_frame is not None or request.end_frame is not None:
        argv += ["in=" + str(request.start_frame if request.start_frame is not None else 0)]
        if request.end_frame is not None:
            argv += ["out=" + str(request.end_frame)]
    argv += [str(project)]
    argv += ["-consumer", *request.consumer_args(output)]
    argv += request.extra_args
    return argv


_MANAGER = RenderManager()


def manager() -> RenderManager:
    """The process wide render manager."""
    return _MANAGER


def render_now(request: RenderRequest, timeout: int = 3600) -> RenderJob:
    """Render synchronously (used by the bridge and by tests)."""
    job = manager().start(request)
    thread = job.thread
    if thread is not None:
        thread.join(timeout=timeout)
    return job


def default_output_path(project: Path, suffix: str = ".mp4") -> Path:
    """A sensible default output path next to the project file."""
    return project.with_suffix(suffix)


def ensure_output_dir(path: Path) -> None:
    """Make sure the directory of an output file exists."""
    path.parent.mkdir(parents=True, exist_ok=True)


def is_writable(path: Path) -> bool:
    """Whether the MCP process can write to ``path``."""
    probe = path if path.exists() else path.parent
    return os.access(probe, os.W_OK)
