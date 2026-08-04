# -*- coding: utf-8 -*-
"""
Whisper audio transcription with Groq → OpenAI fallback.

Downloads audio (yt-dlp), compresses + chunks (ffmpeg), and posts to a
Whisper-compatible API. Defaults to Groq's free `whisper-large-v3` and falls
back to OpenAI's `whisper-1` on HTTP error.
"""

from __future__ import annotations

import ipaddress
import math
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from agent_reach.config import Config

# --- Constants & Limits ---
SIZE_LIMIT_BYTES = 24 * 1024 * 1024  # 24 MiB (Headroom for 25MB API limit)
CHUNK_SECONDS = 600                  # 10 minutes
MAX_SOURCE_BYTES = 512 * 1024 * 1024  # 512 MiB
MAX_CHUNKS = 24                      # ~4 Hours max
MAX_TOTAL_CHUNK_BYTES = 96 * 1024 * 1024
MAX_AUDIO_SECONDS = MAX_CHUNKS * CHUNK_SECONDS
FFPROBE_TIMEOUT_SECONDS = 30
MAX_CONCURRENT_UPLOADS = 4           # Parallel chunk upload concurrency

PROVIDERS: Dict[str, Dict[str, str]] = {
    "groq": {
        "endpoint": "https://api.groq.com/openai/v1/audio/transcriptions",
        "model": "whisper-large-v3",
        "key_field": "groq_api_key",
    },
    "openai": {
        "endpoint": "https://api.openai.com/v1/audio/transcriptions",
        "model": "whisper-1",
        "key_field": "openai_api_key",
    },
}

_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
}


class TranscribeError(RuntimeError):
    """Raised when transcription cannot complete."""


class MissingDependency(TranscribeError):
    """Raised when a required external binary is missing."""


class NoProviderConfigured(TranscribeError):
    """Raised when no provider has an API key configured."""


# --- HTTP Session with Automated Retries ---
def _build_http_session() -> requests.Session:
    """Creates a robust HTTP session with automatic retries for transient failures."""
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist={429, 500, 502, 503, 504},
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    return session


_HTTP_SESSION = _build_http_session()


# --- Validation Helpers ---
def _require(binary: str) -> None:
    if not shutil.which(binary):
        raise MissingDependency(f"Required binary '{binary}' not found in system PATH")


def _require_size_at_most(path: Path, limit: int, label: str) -> int:
    size = path.stat().st_size
    if size > limit:
        limit_mib = limit / (1024 * 1024)
        raise TranscribeError(f"{label} exceeds safety limit of {limit_mib:g} MiB")
    return size


def _probe_audio_duration(path: Path) -> float:
    _require("ffprobe")
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        "-i", str(path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        raise TranscribeError(f"ffprobe timed out after {FFPROBE_TIMEOUT_SECONDS}s")
    except OSError as exc:
        raise TranscribeError(f"ffprobe execution failed: {exc}") from exc

    if proc.returncode != 0:
        detail = proc.stderr.strip()[:300] or "unknown ffprobe error"
        raise TranscribeError(f"ffprobe failed: {detail}")

    try:
        duration = float(proc.stdout.strip())
    except (TypeError, ValueError):
        raise TranscribeError("ffprobe returned invalid duration format") from None

    if not math.isfinite(duration) or duration <= 0:
        raise TranscribeError("ffprobe returned non-positive audio duration")
    return duration


def _require_duration_within_budget(path: Path) -> float:
    duration = _probe_audio_duration(path)
    if duration > MAX_AUDIO_SECONDS:
        max_minutes = MAX_AUDIO_SECONDS // 60
        raise TranscribeError(f"Audio duration exceeds safety limit of {max_minutes} minutes")
    return duration


def _run(cmd: List[str], timeout: int = 600) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TranscribeError(f"{cmd[0]} timed out after {timeout}s")
    if proc.returncode != 0:
        raise TranscribeError(
            f"{cmd[0]} failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}"
        )


def _is_private_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any((
        ip.is_private, ip.is_loopback, ip.is_link_local,
        ip.is_reserved, ip.is_multicast, ip.is_unspecified
    ))


def _assert_safe_public_url(url: str) -> None:
    if "://" not in url:
        parsed = urlparse(f"https://{url}")
    else:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise TranscribeError("SSRF protection: only public http(s) URLs are allowed")

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise TranscribeError("SSRF protection: URL host is missing")
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise TranscribeError("SSRF protection: internal host is restricted")
    if _is_private_ip(host):
        raise TranscribeError("SSRF protection: private/internal IP addresses restricted")


# --- Audio Processing Pipeline ---
def download_audio(url: str, out_dir: Path) -> Path:
    """Download audio safely with yt-dlp into target path."""
    _assert_safe_public_url(url)
    _require("yt-dlp")
    template = out_dir / "source.%(ext)s"
    
    _run(
        [
            "yt-dlp",
            "-x",
            "--audio-format", "m4a",
            "--audio-quality", "0",
            "--no-playlist",
            "--max-filesize", str(MAX_SOURCE_BYTES),
            "-o", str(template),
            "--", url,
        ],
        timeout=1800,
    )
    
    # Filter out .part files to avoid race conditions on downloading streams
    files = [f for f in sorted(out_dir.glob("source.*")) if not f.name.endswith(".part")]
    if not files:
        limit_mib = MAX_SOURCE_BYTES // (1024 * 1024)
        raise TranscribeError(f"yt-dlp produced no output file (limit is {limit_mib} MiB)")
    
    audio = files[0]
    _require_size_at_most(audio, MAX_SOURCE_BYTES, "downloaded source")
    return audio


def compress_audio(src: Path, out_dir: Path) -> Path:
    """Re-encode audio to high-efficiency mono 16kHz/32kbps AAC m4a."""
    _require("ffmpeg")
    dst = out_dir / "compressed.m4a"
    _run([
        "ffmpeg", "-loglevel", "error", "-y",
        "-i", str(src),
        "-t", str(MAX_AUDIO_SECONDS),
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
        str(dst)
    ])
    return dst


def chunk_audio(src: Path, out_dir: Path, segment_seconds: int = CHUNK_SECONDS) -> List[Path]:
    """Split audio into precise segments aligned to keyframes."""
    if segment_seconds <= 0:
        raise TranscribeError("Chunk segment duration must be positive")
    
    _require("ffmpeg")
    pattern = out_dir / "chunk_%03d.m4a"
    _run([
        "ffmpeg", "-loglevel", "error", "-y",
        "-i", str(src),
        "-t", str(MAX_AUDIO_SECONDS),
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-ac", "1", "-ar", "16000", "-b:a", "32k",
        str(pattern)
    ])
    
    chunks = sorted(out_dir.glob("chunk_*.m4a"))
    if not chunks:
        raise TranscribeError("ffmpeg produced no valid chunks")
    return chunks


# --- API Transcribe Dispatcher ---
def _provider_key(provider: str, config: Config) -> Optional[str]:
    field = PROVIDERS[provider]["key_field"]
    return config.get(field) or None


def transcribe_chunk(
    chunk: Path,
    provider: str,
    *,
    config: Optional[Config] = None,
    timeout: int = 120,
) -> str:
    """Transcribe single chunk with specified API provider."""
    if provider not in PROVIDERS:
        raise TranscribeError(f"Unknown provider: {provider}")
    
    cfg = config or Config()
    key = _provider_key(provider, cfg)
    if not key:
        raise NoProviderConfigured(
            f"{provider}: missing key '{PROVIDERS[provider]['key_field']}'"
        )

    info = PROVIDERS[provider]
    with chunk.open("rb") as fh:
        try:
            resp = _HTTP_SESSION.post(
                info["endpoint"],
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (chunk.name, fh, "audio/m4a")},
                data={"model": info["model"], "response_format": "text"},
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise TranscribeError(f"{provider}: network error: {e}") from e

    if not resp.ok:
        raise TranscribeError(f"{provider}: HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.text


def _provider_order(provider: str) -> List[str]:
    if provider == "auto":
        return ["groq", "openai"]
    if provider in PROVIDERS:
        return [provider]
    raise TranscribeError(f"Invalid provider: {provider} (expected: groq | openai | auto)")


def _transcribe_with_fallback(chunk: Path, order: List[str], config: Config) -> str:
    """Attempt execution across providers in waterfall sequence."""
    last_err: Optional[Exception] = None
    for p in order:
        if not _provider_key(p, config):
            continue
        try:
            return transcribe_chunk(chunk, p, config=config)
        except TranscribeError as e:
            last_err = e
            continue
    raise TranscribeError(f"All providers failed for {chunk.name}: {last_err}")


# --- Primary Entry Points ---
def transcribe(
    source: str,
    *,
    provider: str = "auto",
    out_dir: Optional[Path] = None,
    config: Optional[Config] = None,
    parallel: bool = True,
) -> str:
    """Transcribe a local file or remote URL into complete transcript text.

    Args:
        source: URL or filesystem path to target audio.
        provider: Provider selection strategy ("auto", "groq", "openai").
        out_dir: Custom work directory (uses safe temporary directory if None).
        config: AgentReach Config instance.
        parallel: Enable multi-threaded concurrent chunk processing.
    """
    cfg = config or Config()
    order = _provider_order(provider)

    if not any(_provider_key(p, cfg) for p in order):
        names = ", ".join(PROVIDERS[p]["key_field"] for p in order)
        raise NoProviderConfigured(f"No valid API keys configured (expected one of: {names})")

    if out_dir:
        return _transcribe_in_dir(source, order, cfg, Path(out_dir), parallel=parallel)

    with tempfile.TemporaryDirectory(prefix="transcribe-") as tmp:
        return _transcribe_in_dir(source, order, cfg, Path(tmp), parallel=parallel)


def _transcribe_in_dir(
    source: str,
    order: List[str],
    cfg: Config,
    work_dir: Path,
    parallel: bool = True,
) -> str:
    work_dir.mkdir(parents=True, exist_ok=True)
    src_path = Path(source)
    audio = src_path if src_path.is_file() else download_audio(source, work_dir)

    _require_size_at_most(audio, MAX_SOURCE_BYTES, "source")
    _require_duration_within_budget(audio)

    compressed = compress_audio(audio, work_dir)
    chunks = (
        [compressed]
        if compressed.stat().st_size <= SIZE_LIMIT_BYTES
        else chunk_audio(compressed, work_dir)
    )

    if len(chunks) > MAX_CHUNKS:
        max_minutes = MAX_CHUNKS * CHUNK_SECONDS // 60
        raise TranscribeError(
            f"Audio produced {len(chunks)} chunks (max allowed is {MAX_CHUNKS}, ~{max_minutes} mins)"
        )

    # Validate chunk bounds
    total_chunk_bytes = sum(
        _require_size_at_most(chunk, SIZE_LIMIT_BYTES, f"chunk {chunk.name}")
        for chunk in chunks
    )
    if total_chunk_bytes > MAX_TOTAL_CHUNK_BYTES:
        limit_mib = MAX_TOTAL_CHUNK_BYTES / (1024 * 1024)
        raise TranscribeError(
            f"Total chunk batch size {total_chunk_bytes} bytes exceeds {limit_mib:g} MiB limit"
        )

    # Process chunks (Parallel vs Sequential)
    results: List[str] = [""] * len(chunks)
    if parallel and len(chunks) > 1:
        with ThreadPoolExecutor(max_workers=min(len(chunks), MAX_CONCURRENT_UPLOADS)) as executor:
            future_to_idx = {
                executor.submit(_transcribe_with_fallback, chunk, order, cfg): i
                for i, chunk in enumerate(chunks)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                results[idx] = future.result().strip()
    else:
        for i, chunk in enumerate(chunks):
            results[i] = _transcribe_with_fallback(chunk, order, cfg).strip()

    return "\n".join(p for p in results if p)
