"""Paths, config, logging and the small helpers every command needs."""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
from pathlib import Path

log = logging.getLogger("meeting_transcriber")

PACKAGE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("MEETING_TRANSCRIBER_HOME")
            or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            / "meeting-transcriber").expanduser()
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "meeting-transcriber"
CONFIG = HOME / "config.yaml"
ENV = HOME / ".env"

# Picked up from the inbox folder. A file passed by path can be anything ffmpeg
# can decode, whatever its extension.
MEDIA_EXTS = {
    ".3g2", ".3gp", ".aac", ".ac3", ".aif", ".aifc", ".aiff", ".amr", ".ape", ".au",
    ".avi", ".caf", ".flac", ".flv", ".m2ts", ".m4a", ".m4b", ".m4v", ".mka", ".mkv",
    ".mov", ".mp2", ".mp3", ".mp4", ".mpeg", ".mpg", ".mpga", ".mts", ".oga", ".ogg",
    ".opus", ".ra", ".spx", ".ts", ".wav", ".weba", ".webm", ".wma", ".wmv", ".wv",
}

DEFAULTS = {
    "output_dir": "~/Meetings",
    "inbox": "~/Meetings/inbox",
    "transcription_model": "scribe_v2",
    "summary": True,
    "summary_language": "auto",
}


class Error(Exception):
    """A failure the user can act on. Printed as `error: <message>`, no traceback."""


# ── logging ──────────────────────────────────────────────────────────────────
# Progress and diagnostics go to stderr through `log`; results (file paths, the
# short summary) go to stdout with print(), so output can be piped or captured.

class _Formatter(logging.Formatter):
    COLORS = {logging.DEBUG: "2", logging.WARNING: "33", logging.ERROR: "31"}

    def __init__(self, color: bool):
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.levelno != logging.INFO:
            msg = f"{record.levelname.lower()}: {msg}"
        if self.color and record.levelno in self.COLORS:
            msg = f"\033[{self.COLORS[record.levelno]}m{msg}\033[0m"
        return msg


def setup_logging(level: int = logging.INFO) -> None:
    color = sys.stderr.isatty() and not os.environ.get("NO_COLOR")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter(color))
    log.handlers[:] = [handler]
    log.setLevel(level)
    log.propagate = False


def verbosity_flags() -> argparse.ArgumentParser:
    """-v / -q, shared by every subcommand."""
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_mutually_exclusive_group()
    g.add_argument("-v", "--verbose", action="store_true", help="show debug output")
    g.add_argument("-q", "--quiet", action="store_true", help="only warnings and errors")
    return p


def apply_verbosity(args: argparse.Namespace) -> None:
    if args.verbose:
        log.setLevel(logging.DEBUG)
    elif args.quiet:
        log.setLevel(logging.WARNING)


# ── config ───────────────────────────────────────────────────────────────────

def load_env() -> None:
    """Minimal .env loader. Variables already set in the environment win."""
    if not ENV.exists():
        return
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in ('"', "'"):
            v = v[1:].split(v[0], 1)[0]
        else:
            v = v.split(" #", 1)[0].strip()   # an inline comment
        if v:
            os.environ.setdefault(k.strip(), v)


def load_config() -> dict:
    import yaml

    if not CONFIG.exists():
        raise Error(f"no config at {CONFIG}. Run `transcribe init` to create one.")
    try:
        cfg = yaml.safe_load(CONFIG.read_text()) or {}
    except yaml.YAMLError as e:
        raise Error(f"{CONFIG} is not valid YAML: {e}") from None
    return {**DEFAULTS, **cfg}


def settings_for(cfg: dict, project: dict | None) -> dict:
    """Top-level settings with the project's own overrides on top."""
    base = {k: v for k, v in cfg.items() if k not in ("projects", "llm")}
    return {"name": "", "speakers": [], "roles": {}, "context": "", "keyterms": [],
            **base, **(project or {})}


def find_project(projects: dict, name: str) -> tuple[str, dict]:
    """By key (`personal`) or by display name (`Personal`), case-insensitive."""
    for key, p in projects.items():
        if name.casefold() in (key.casefold(), str(p.get("name", "")).casefold()):
            return key, p
    known = ", ".join(projects) or "none defined yet"
    raise Error(f"unknown project {name!r}. Known: {known} (see `transcribe projects`)")


# ── small helpers ────────────────────────────────────────────────────────────

def require(cmd: str) -> None:
    if shutil.which(cmd) is None:
        raise Error(f"{cmd} not found on PATH. Install it first "
                    f"(macOS: brew install {cmd}, Debian/Ubuntu: sudo apt install {cmd}).")


def slugify(text: str) -> str:
    """'Interview with Sam' -> 'interview-with-sam'. Non-Latin letters are kept."""
    s = re.sub(r"[^\w\s-]", "", text.strip().lower(), flags=re.UNICODE)
    return re.sub(r"[\s_]+", "-", s).strip("-")


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 2
    while (cand := path.with_stem(f"{path.stem}-{i}")).exists():
        i += 1
    return cand
