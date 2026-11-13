"""
The pipeline behind `transcribe <audio>`:

  audio or video file(s) -> ffmpeg (one mono track)
  -> ElevenLabs Scribe (words, timestamps, anonymous speaker labels)
  -> the LLM names the labels (small JSON reply)
  -> transcript.md, rendered deterministically: the LLM never rewrites the text
  -> summaries (summarize.py)
  -> the recording is filed next to them, one folder per meeting

A run that stops part way is finished by running the same command again: the
audio stays where it was until the very end, and the transcription is cached.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import llm, summarize
from .common import (CACHE, MEDIA_EXTS, Error, apply_verbosity,
                     find_project, load_config, log, require, settings_for, slugify,
                     unique_path, verbosity_flags)

MAP_CHAR_CAP = 120_000  # cap on transcript text sent to the LLM for speaker naming
KEYTERM_CAP = 100       # ElevenLabs caps the keyterm list


# ── setup ────────────────────────────────────────────────────────────────────

def parse_people(spec: str) -> tuple[list[str], dict]:
    """'Alice: designer, Bob, Carol: CEO' -> (names, {name: role})."""
    names: list[str] = []
    roles: dict = {}
    for item in spec.split(","):
        name, _, role = item.partition(":")
        name = name.strip()
        if not name:
            continue
        names.append(name)
        if role.strip():
            roles[name] = role.strip()
    return names, roles


# ── audio ────────────────────────────────────────────────────────────────────

def media_in(folder: Path, recursive: bool = False) -> list[Path]:
    if not folder.is_dir():
        return []
    return [f for f in (folder.rglob("*") if recursive else folder.iterdir())
            if f.suffix.lower() in MEDIA_EXTS and f.is_file()]


def collect_audio(paths: list[str], inbox: Path) -> list[Path]:
    if paths:
        files = [Path(p).expanduser().resolve() for p in paths]
        for f in files:
            if not f.is_file():
                raise Error(f"file not found: {f}")
        return files
    files = sorted(media_in(inbox))
    if not files:
        raise Error(f"no audio given and nothing in the inbox ({inbox}).\n"
                    "Pass a file: transcribe path/to/recording.m4a")
    return files


def infer_date(files: list[Path]) -> str:
    """A YYYY-MM-DD file name beats the file's mtime (the weaker signal: saving a
    file from a chat app stamps it with today, not the recording's date)."""
    named = [m.group(1) for f in files
             if (m := re.match(r"(\d{4}-\d{2}-\d{2})", f.stem))]
    if named:
        return min(named)
    ts = min(f.stat().st_mtime for f in files)
    return dt.date.fromtimestamp(ts).isoformat()


def fingerprint(files: list[Path]) -> str:
    """Stable key for a set of source files, used to cache the transcription."""
    h = hashlib.sha1()
    for f in files:
        with open(f, "rb") as fh:
            h.update(hashlib.file_digest(fh, "sha1").digest())
    return h.hexdigest()[:16]


def prepare_audio(files: list[Path], workdir: Path) -> Path:
    """Decode any audio or video file(s) ffmpeg understands into one mono Opus
    track. Several files are joined end to end, in the order given."""
    out = workdir / "audio.ogg"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for f in files:
        cmd += ["-i", str(f)]
    n = len(files)
    chains = "".join(f"[{i}:a:0]aresample=48000,aformat=channel_layouts=mono[a{i}];"
                     for i in range(n))
    inputs = "".join(f"[a{i}]" for i in range(n))
    cmd += ["-filter_complex", f"{chains}{inputs}concat=n={n}:v=0:a=1[out]",
            "-map", "[out]", "-c:a", "libopus", "-b:a", "48k", str(out)]
    log.debug("ffmpeg: %s", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise Error("ffmpeg could not read the input. Is every file an audio or video "
                    f"file with a sound track?\n{r.stderr.strip()[-1500:]}")
    return out


def duration(path: Path) -> float | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else \
        f"{s // 60}:{s % 60:02d}"


# ── transcription ────────────────────────────────────────────────────────────

def build_keyterms(settings: dict, speakers: list[str]) -> list[str]:
    """Names plus the project glossary, deduplicated. These are the words speech
    recognition gets wrong most (brands, jargon, names), so biasing them helps."""
    terms: list[str] = []
    for t in [*speakers, *(settings.get("keyterms") or [])]:
        t = str(t).strip()
        if t and t not in terms:
            terms.append(t)
    if len(terms) > KEYTERM_CAP:
        log.warning("%d keyterms, keeping the first %d", len(terms), KEYTERM_CAP)
        terms = terms[:KEYTERM_CAP]
    return terms


def scribe(audio: Path, num_speakers: int | None, keyterms: list[str],
           model: str) -> list[dict]:
    """ElevenLabs Scribe with diarization, language auto-detected -> word dicts."""
    from elevenlabs.client import ElevenLabs

    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise Error("ELEVENLABS_API_KEY is not set. Put it in the .env file "
                    "(see `transcribe init`).")
    log.info("Transcribing with ElevenLabs %s (%.1f MB, %s, %d keyterms). "
             "Long recordings take a few minutes.", model, audio.stat().st_size / 1e6,
             f"up to {num_speakers} speakers" if num_speakers else "speakers auto",
             len(keyterms))
    client = ElevenLabs(api_key=key)
    try:
        with open(audio, "rb") as fh:
            resp = client.speech_to_text.convert(
                file=fh, model_id=model, diarize=True,
                num_speakers=num_speakers or None, keyterms=keyterms or None)
    except Exception as e:
        raise Error(f"ElevenLabs transcription failed: {e}") from None
    lang = getattr(resp, "language_code", None)
    prob = getattr(resp, "language_probability", None)
    log.info("Detected language: %s%s", lang or "unknown",
             f" ({prob:.0%})" if isinstance(prob, float) else "")
    words = getattr(resp, "words", None)
    if not words:
        raise Error("ElevenLabs returned no words. Is there speech in the recording?")
    return [w if isinstance(w, dict) else w.model_dump() for w in words]


def build_turns(words: list[dict]) -> list[dict]:
    """Group consecutive words by speaker_id into turns [{speaker, text}]."""
    turns: list[dict] = []
    for w in words:
        text = w.get("text", "") or ""
        wtype = w.get("type", "word")
        spk = w.get("speaker_id") or "speaker_0"
        if wtype == "word":
            if not turns or turns[-1]["speaker"] != spk:
                turns.append({"speaker": spk, "text": text})
            else:
                turns[-1]["text"] += text
        elif turns:  # spacing and punctuation attach to the current turn
            turns[-1]["text"] += text
    for t in turns:
        t["text"] = t["text"].strip()
    return [t for t in turns if t["text"]]


# ── speaker naming (LLM) ─────────────────────────────────────────────────────

MAP_PROMPT = """You are labelling a recorded conversation by speaker.

Project: {name}
Context: {context}
{roster}
{roles}{pinned}
Diarization produced {n_labels} labels. Statistics per label:
{stats}
IMPORTANT: there are often MORE labels than real people, because diarization
splits one voice into several labels. Labels with few words and few turns are
almost always such fragments. Give a fragment to the person whose speech it
interrupts (look at who speaks around it and whether the thought continues).

Work in two steps.

STEP 1. Collect DIRECT EVIDENCE, not a general impression:
- being addressed by name ("Sam, what do you think?"): usually the next label answers;
- self-identification ("as the co-founder, I...", "I built that part");
- grammatical gender, in languages that mark it (verb or adjective endings in the
  first person), checked against each name's likely gender;
- who presents, who asks, who is on the client side, from the roles above.

STEP 2. Assign names to labels from that evidence.

Rules:
{rules}
- Several labels for one person is normal and expected.
- Do not put a woman's voice on a man's name or the other way round. This is the
  most common mistake.
- confidence: "high" ONLY with direct evidence (addressed by name or
  self-identification). "medium" for an inference from roles or content. "low" for
  a guess. An honest "low" is far more useful than a confident mistake.
- evidence: a short quote or reason (up to 100 characters), in the language of the
  conversation.

Reply with ONLY a JSON object, no Markdown, no explanation:
{{"speaker_0": {{"name": "Name", "confidence": "high", "evidence": "'Sam, show the slide', speaker_0 answers"}},
 "speaker_1": {{"name": "Name", "confidence": "low", "evidence": "fragment, 1 turn inside speaker_0's speech"}}}}

Transcript:
{transcript}
"""

ROSTER_RULES = """- Give each label exactly one name, strictly from the participant list.
- If there are fewer voices than names on the list, leave the extra names unused."""

OPEN_RULES = """- Nobody told you who was on this call. Use a person's name only when the
  conversation makes it clear (they introduce themselves, or they are addressed by
  name and answer). Otherwise keep the label id (e.g. "speaker_1") as the name.
- Never invent a name."""


def sample_for_mapping(turns: list[dict]) -> str:
    text = "\n".join(f"{t['speaker']}: {t['text']}" for t in turns)
    if len(text) <= MAP_CHAR_CAP:
        return text
    return f"{text[: MAP_CHAR_CAP * 3 // 4]}\n...\n{text[-MAP_CHAR_CAP // 4:]}"


def speaker_stats(turns: list[dict]) -> dict:
    """Per-label word and turn counts plus the longest turn: the signal that
    exposes diarization fragments (few words spread over few turns)."""
    stats: dict = {}
    for t in turns:
        s = stats.setdefault(t["speaker"], {"words": 0, "turns": 0, "longest": ""})
        s["words"] += len(t["text"].split())
        s["turns"] += 1
        if len(t["text"]) > len(s["longest"]):
            s["longest"] = t["text"]
    return stats


def parse_mapping(raw: str, ids: list[str], pinned: dict | None = None,
                  pin_note: dict | None = None) -> dict:
    """The model's JSON reply -> {speaker_id: {"name", "confidence", "evidence"}}.
    `pinned` labels keep their given name whatever the reply says."""
    pinned, pin_note = pinned or {}, pin_note or {}
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise Error(f"speaker naming: no JSON in the LLM reply:\n{raw[:500]}")
    try:
        reply = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise Error(f"speaker naming: invalid JSON from the LLM ({e}):\n"
                    f"{m.group(0)[:500]}") from None

    out: dict = {}
    for sid in ids:
        if sid in pinned:
            out[sid] = {"name": pinned[sid], "confidence": "pinned",
                        "evidence": pin_note.get(sid, "")}
            continue
        v = reply.get(sid)
        if isinstance(v, str):
            v = {"name": v, "confidence": "medium", "evidence": ""}
        if not isinstance(v, dict) or not v.get("name"):
            # nothing usable: keep the raw label rather than invent a name
            out[sid] = {"name": sid, "confidence": "low", "evidence": "not labelled"}
            continue
        conf = str(v.get("confidence", "low")).lower()
        out[sid] = {"name": str(v["name"]),
                    "confidence": conf if conf in ("high", "medium", "low") else "low",
                    "evidence": str(v.get("evidence") or "")}
    return out


def map_speakers(turns: list[dict], settings: dict, llm_cfg: dict, pinned: dict | None = None,
                 pin_note: dict | None = None) -> dict:
    """Name every diarization label. `pinned` ({label: name}) is what a host program
    already knows: given to the model as fact and passed straight through. Part of
    the library API (lifey pins the names a user filed with), so keep it stable."""
    ids = sorted({t["speaker"] for t in turns})
    stats = speaker_stats(turns)
    pinned, pin_note = pinned or {}, pin_note or {}
    if all(sid in pinned for sid in ids):     # nothing left to name
        return parse_mapping("{}", ids, pinned, pin_note)

    speakers = settings.get("speakers") or []
    log.info("Naming %d speaker label(s) with %s", len(ids), llm_cfg.get("model"))
    roles = settings.get("roles") or {}
    roles_block = ""
    if roles:
        lines = "\n".join(f"- {name}: {desc}" for name, desc in roles.items())
        roles_block = f"Who is who (use it to tell people apart):\n{lines}\n"
    pinned_block = ""
    if pinned:
        lines = "\n".join(f"- {sid} = {name} ({pin_note.get(sid, 'given')})"
                          for sid, name in sorted(pinned.items()))
        pinned_block = ("ALREADY KNOWN FOR CERTAIN (do not revise, use as anchors "
                        f"for the rest):\n{lines}\n")
    prompt = MAP_PROMPT.format(
        name=settings.get("name") or "-",
        context=" ".join((settings.get("context") or "-").split()),
        roster=f"Participants: {', '.join(speakers)}" if speakers
        else "Participants: unknown",
        roles=roles_block,
        pinned=pinned_block,
        n_labels=len(ids),
        stats="\n".join(f"- {sid}: {stats[sid]['words']} words, {stats[sid]['turns']} turns"
                        for sid in ids),
        rules=ROSTER_RULES if speakers else OPEN_RULES,
        transcript=sample_for_mapping(turns),
    )
    return parse_mapping(llm.complete(llm_cfg, prompt), ids, pinned, pin_note)


def review_mapping(mapping: dict, stats: dict) -> list[str]:
    """One line per label; evidence and a speech sample only where it is shaky,
    which is the only place worth checking. Returns the shaky labels."""
    shaky = [s for s, m in mapping.items()
             if m["confidence"] == "low" and m["name"] != s]
    for sid in sorted(mapping, key=lambda s: -stats.get(s, {}).get("words", 0)):
        m, st = mapping[sid], stats.get(sid, {})
        log.info("  %-12s %6d words %4d turns  ->  %-16s %s", sid, st.get("words", 0),
                 st.get("turns", 0), speaker_label(mapping, sid), m["confidence"])
        if sid in shaky:
            if m["evidence"]:
                log.info("      why: %s", m["evidence"][:100])
            if sample := " ".join((st.get("longest") or "").split())[:110]:
                log.info('      e.g. "%s..."', sample)
    return shaky


def review_roster(mapping: dict, speakers: list[str], stats: dict) -> None:
    """The roster says these people were on the call. Someone in it who never got
    a label was silent, dropped by diarization, or misattributed: all worth
    seeing before trusting the transcript."""
    named = {m["name"] for m in mapping.values()}
    unnamed = sorted(s for s, m in mapping.items() if m["name"] == s)
    if speakers:
        if missing := [s for s in speakers if s not in named]:
            log.warning("on the list but never heard: %s. Either they were silent or "
                        "their lines went to someone else; if they were not on the "
                        "call, re-run with --drop-speakers.", ", ".join(missing))
        if extra := sorted(n for n in named if n not in speakers and n not in mapping):
            log.info("Named from the conversation, not on the list: %s", ", ".join(extra))
    if unnamed:
        words = sum(stats.get(s, {}).get("words", 0) for s in unnamed)
        log.info("%d speaker(s) left unnamed (%d words). Name them with --speakers.",
                 len(unnamed), words)


# ── render and file ──────────────────────────────────────────────────────────

def speaker_label(mapping: dict, sid: str) -> str:
    """`Alice` when confident, `Alice?` when it is a guess, so a wrong attribution
    is visible in the file instead of reading as fact. Unnamed labels become
    `Speaker 1`, `Speaker 2`, ..."""
    m = mapping.get(sid)
    name = m["name"] if m else sid
    if num := re.fullmatch(r"speaker_(\d+)", name):
        return f"Speaker {int(num.group(1)) + 1}"
    return f"{name}?" if m and m["confidence"] == "low" else name


def render_md(turns: list[dict], mapping: dict) -> str:
    """Apply the mapping, merge consecutive turns of one speaker, `Name: text`."""
    merged: list[dict] = []
    for t in turns:
        name = speaker_label(mapping, t["speaker"])
        if merged and merged[-1]["name"] == name:
            merged[-1]["text"] += " " + t["text"]
        else:
            merged.append({"name": name, "text": t["text"]})
    return "\n\n".join(f"{t['name']}: {t['text']}" for t in merged) + "\n"


def render_frontmatter(date: str, title: str, description: str, speakers: list[str]) -> str:
    """YAML block marking the file as a transcript, for folders that mix
    transcripts with hand-written notes (`frontmatter: true`)."""
    lines = ["---", "type: transcript", f"date: {date}"]
    if title:
        lines.append(f"title: {json.dumps(title, ensure_ascii=False)}")
    if description:
        lines.append(f"description: {json.dumps(description, ensure_ascii=False)}")
    if speakers:
        lines.append(f"participants: {json.dumps(speakers, ensure_ascii=False)}")
    lines += ["---", "", ""]
    return "\n".join(lines)


def resolve_meeting(meetings_dir: Path, stem: str, flat: bool, files: list[Path],
                    fp: str) -> tuple[Path, str, bool]:
    """Where this audio's meeting lives -> (folder, stem, resumed).

    One audio, one meeting. The folder picked on the first run is remembered in
    the cache, so a run that was stopped goes back into the same meeting instead
    of opening `<date>-2/`. An already archived recording passed as input lands
    back in its own meeting the same way.

    `flat` output folders (a notes folder, say) have no per-meeting folder: the
    stem alone names the meeting, next to hand-written notes.
    """
    idx = CACHE / f"{fp}.meeting.json"
    if idx.exists():
        m = json.loads(idx.read_text())
        return Path(m["dir"]), m["stem"], True
    one = files[0]
    if len(files) == 1:                                   # an archived recording?
        if flat and one.parent == meetings_dir / "recordings":
            return meetings_dir, one.stem, True
        if not flat and one.parent.parent == meetings_dir:
            return one.parent, one.parent.name, True
    if flat:
        # ponytail: a stem claimed by a stopped flat run that never wrote its .md can
        # be handed out again; check recordings/ too if that ever bites
        mtg, stem = meetings_dir, unique_path(meetings_dir / f"{stem}.md").stem
    else:
        mtg = unique_path(meetings_dir / stem)
        stem = mtg.name
    mtg.mkdir(parents=True, exist_ok=True)   # claim the name before anything slow runs
    idx.write_text(json.dumps({"dir": str(mtg), "stem": stem}, ensure_ascii=False))
    return mtg, stem, False


def write_transcript(md: str, mdir: Path, stem: str, flat: bool) -> Path:
    mdir.mkdir(parents=True, exist_ok=True)
    out = mdir / (f"{stem}.md" if flat else "transcript.md")
    out.write_text(md, encoding="utf-8")
    return out


def archive_meeting(sources: list[Path], meeting_audio: Path, mdir: Path, stem: str,
                    flat: bool, inbox: Path) -> Path:
    """File exactly ONE recording per meeting, beside its transcript. Inbox files
    are moved; files passed by path are copied and the original is left alone."""
    rec_dir = mdir / "recordings" if flat else mdir
    name = stem if flat else "recording"
    rec_dir.mkdir(parents=True, exist_ok=True)
    if len(sources) == 1:
        src = sources[0]
        dest = rec_dir / f"{name}{src.suffix.lower()}"
        if src.resolve() == dest.resolve():
            return dest                      # re-run of an archived recording
        if inbox in src.parents:
            shutil.move(str(src), dest)
        else:
            shutil.copy2(src, dest)
    else:
        # several files were joined: keep the single joined recording
        dest = rec_dir / f"{name}.ogg"
        shutil.copy2(meeting_audio, dest)
        for src in sources:
            if inbox in src.parents:
                src.unlink()
    return dest


# ── guided mode: `transcribe` with nothing to do asks instead ────────────────

PLACES = [("~/Downloads", False), ("~/Desktop", False), ("~/Movies", False),
          ("~/Documents/Zoom", True), ("~/Music/Voice Memos", False)]


def recent_recordings(limit: int = 9) -> list[Path]:
    files = [f for place, deep in PLACES for f in media_in(Path(place).expanduser(), deep)]
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)[:limit]


def as_path(text: str) -> Path:
    """A path typed or dragged into the terminal: quoted, or with escaped spaces."""
    text = text.strip()
    if text[:1] in "'\"" and text[-1:] == text[:1]:
        text = text[1:-1]
    return Path(re.sub(r"\\(.)", r"\1", text)).expanduser()


def choose(prompt: str, count: int, default: int | None) -> int | None:
    """A number from 1 to count, or the default on Enter. q quits."""
    while True:
        answer = input(prompt).strip().lower()
        if answer in ("q", "quit", "exit"):
            sys.exit(0)
        if not answer:
            return default
        if answer.isdigit() and 0 <= int(answer) <= count:
            return int(answer)
        print(f"  type a number from 1 to {count}, Enter for the default, or q to quit")


def guide(args: argparse.Namespace, cfg: dict) -> None:
    """Ask for the file, the project and the speakers, with Enter as the shortcut."""
    recent = recent_recordings()
    if recent:
        print("Which recording? (a number, or drag a file in; Enter = 1, q = quit)")
    else:
        print("No recordings in " + ", ".join(p for p, _ in PLACES) +
              ".\nDrag a file in, or type its path (q = quit)")
    home = str(Path.home())
    for i, f in enumerate(recent, 1):
        when = dt.datetime.fromtimestamp(f.stat().st_mtime)
        print(f"  {i}  {when:%Y-%m-%d %H:%M}  {f.stat().st_size / 1e6:6.1f} MB  "
              f"{str(f).replace(home, '~', 1)}")
    while not args.audio:
        answer = input("> ").strip()
        if answer.lower() in ("q", "quit", "exit"):
            sys.exit(0)
        if not answer or answer.isdigit():
            n = int(answer or 1)
            if 1 <= n <= len(recent):
                args.audio = [str(recent[n - 1])]
                continue
        elif (p := as_path(answer)).is_file():
            args.audio = [str(p)]
            continue
        print("  not a file; drag it in from Finder, or type its number")

    projects = cfg.get("projects") or {}
    if projects and not args.project:
        keys = list(projects)
        print("Project? (Enter = none)")
        for i, key in enumerate(keys, 1):
            print(f"  {i}  {key}  {', '.join(map(str, projects[key].get('speakers') or []))}")
        if n := choose("> ", len(keys), None):
            args.project = keys[n - 1]
    if not args.project and not args.speakers:
        args.speakers = input("Who was on the call? (e.g. Alice, Sam: client; "
                              "Enter = work it out) > ").strip()


# ── main ─────────────────────────────────────────────────────────────────────

EXAMPLES = """examples:
  transcribe                                  asks which recording, then goes
  transcribe call.m4a -s "Alice, Sam: client"
  transcribe -p work standup.mp3 -d Carol     project 'work', Carol was away
  transcribe part1.mp3 part2.mp3 -n "Interview with Sam"
  transcribe zoom.mp4 -l English -o .

other commands:
  transcribe init                             first-time setup
  transcribe doctor                           check the setup
  transcribe projects [NAME]                  projects in config.yaml
  transcribe summarize TRANSCRIPT             redo the summary of a transcript
  transcribe record                           record a call on macOS
"""


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="transcribe", parents=[verbosity_flags()], epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Transcribe a recording into a Markdown transcript with named "
                    "speakers, plus a summary.")
    ap.add_argument("audio", nargs="*",
                    help="audio or video file(s), any format; several are joined in "
                         "order (default: the inbox folder, else asks)")
    ap.add_argument("-p", "--project", help="project from config.yaml")
    ap.add_argument("-s", "--speakers", default="",
                    help="who is on the call, e.g. 'Alice, Sam: client'")
    ap.add_argument("-d", "--drop-speakers", default="", metavar="NAMES",
                    help="people on the project's list who were not on this call")
    ap.add_argument("-n", "--name", default="",
                    help="what it is, e.g. 'Interview with Sam' (names the folder)")
    ap.add_argument("-l", "--language", metavar="LANG",
                    help="language of the summaries (default: the conversation's)")
    ap.add_argument("-o", "--output", metavar="DIR", help="put the meeting folder here")
    ap.add_argument("--no-summary", action="store_true", help="transcript only")
    ap.add_argument("--json", action="store_true",
                    help="print the results as JSON on stdout (for scripts and agents)")
    ap.add_argument("--retranscribe", action="store_true",
                    help="ignore the cached transcription (ElevenLabs charges again)")
    ap.add_argument("--description", default="", help=argparse.SUPPRESS)
    return ap


def run(argv: list[str]) -> None:
    args = parser().parse_args(argv)
    apply_verbosity(args)
    cfg = load_config()
    require("ffmpeg")
    inbox = Path(settings_for(cfg, None)["inbox"]).expanduser().resolve()
    if not args.audio and not media_in(inbox) and sys.stdin.isatty() and not args.json:
        guide(args, cfg)
    project = find_project(cfg.get("projects") or {}, args.project)[1] if args.project \
        else None
    settings = settings_for(cfg, project)
    llm_cfg = cfg.get("llm") or {}

    # who is on the call: project roster, plus --speakers, minus --drop-speakers
    speakers = [str(s) for s in settings.get("speakers") or []]
    roles = dict(settings.get("roles") or {})
    extra, extra_roles = parse_people(args.speakers)
    speakers += [n for n in extra if n not in speakers]
    roles.update(extra_roles)
    if args.drop_speakers:
        drop = set(parse_people(args.drop_speakers)[0])
        if unknown := drop - set(speakers):
            log.warning("--drop-speakers: not on the list, ignored: %s",
                        ", ".join(sorted(unknown)))
        speakers = [n for n in speakers if n not in drop]
        roles = {n: r for n, r in roles.items() if n not in drop}
    settings = {**settings, "speakers": speakers, "roles": roles}
    if args.output:
        settings["output_dir"] = args.output

    inbox = Path(settings["inbox"]).expanduser().resolve()
    files = collect_audio(args.audio, inbox)
    date = infer_date(files)
    slug = slugify(args.name)
    stem = f"{date}-{slug}" if slug else date
    out_dir = Path(settings["output_dir"]).expanduser().resolve()
    flat = bool(settings.get("flat"))
    CACHE.mkdir(parents=True, exist_ok=True)
    fp = fingerprint(files)
    mtg, stem, resumed = resolve_meeting(out_dir, stem, flat, files, fp)
    if m := re.match(r"\d{4}-\d{2}-\d{2}", stem):   # a resumed meeting keeps its date
        date = m.group(0)
    log.info("%s%s, %d file(s), speakers: %s", f"{settings['name']}: " if settings["name"]
             else "", stem, len(files), ", ".join(speakers) or "unknown")
    if resumed:
        log.info("Continuing the existing meeting in %s", mtg)

    with tempfile.TemporaryDirectory() as tmp:
        audio = prepare_audio(files, Path(tmp))
        if secs := duration(audio):
            log.info("Audio: %s", hms(secs))

        # num_speakers is a cap for Scribe, not a hint: a roster of one would tell
        # it not to diarize at all, so below two it works the count out itself
        params = {"num_speakers": len(speakers) if len(speakers) > 1 else None}
        model = settings["transcription_model"]
        cache_file = CACHE / f"{fp}.scribe.json"
        cached = json.loads(cache_file.read_text()) if cache_file.exists() else None
        if cached and not args.retranscribe:
            if cached["params"] != params:
                raise Error("this audio was transcribed before with a different number "
                            f"of speakers ({cached['params']['num_speakers'] or 'auto'}). "
                            "Pass --retranscribe to transcribe it again with the new list "
                            "(ElevenLabs charges again).")
            # caches from before the model was recorded were all made with scribe_v2
            was = cached.get("model", "scribe_v2")
            if was != model:
                raise Error(f"this audio was transcribed before with {was}, the config now "
                            f"says {model}. Pass --retranscribe to transcribe it again "
                            f"with {model} (ElevenLabs charges again).")
            log.info("Using the cached %s transcription (no charge)", model)
            words = cached["words"]
        else:
            words = scribe(audio, params["num_speakers"], build_keyterms(settings, speakers),
                           model)
            cached = {"params": params, "model": model, "words": words}
            cache_file.write_text(json.dumps(cached, ensure_ascii=False))

        turns = build_turns(words)
        stats = speaker_stats(turns)
        mapping = map_speakers(turns, settings, llm_cfg)
        shaky = review_mapping(mapping, stats)
        review_roster(mapping, speakers, stats)

        md = render_md(turns, mapping)
        if settings.get("frontmatter"):
            spoke = {m["name"] for m in mapping.values()}
            md = render_frontmatter(date, args.name, args.description,
                                    [s for s in speakers if s in spoke]) + md
        out = write_transcript(md, mtg, stem, flat)
        log.info("Transcript written: %d lines", len(md.splitlines()))
        if shaky:
            log.warning('%d speaker(s) uncertain, marked with "?" in the transcript',
                        len(shaky))

        results = [("transcript", out)]
        if settings["summary"] and not args.no_summary:
            language = args.language or settings["summary_language"]
            try:
                long_out, short_out = summarize.summarize_transcript(
                    md, out, settings, llm_cfg, date, language, show=not args.json)
            except Error as e:
                raise Error(f"{e}\nThe transcript is saved at {out}. Run the same command "
                            "again to retry: the transcription is cached.") from None
            results.append(("summary", long_out))
            if short_out:
                results.append(("short", short_out))

        # Last on purpose: until here the audio is still where it was, so a run
        # stopped anywhere above is finished by running the same command again.
        dest = archive_meeting(files, audio, mtg, stem, flat, inbox)
        results.append(("recording", dest))
        if len(files) > 1:
            # the cache is keyed on the source files and they are gone now: store it
            # under the joined recording too, so re-running on it costs nothing
            (CACHE / f"{fingerprint([dest])}.scribe.json").write_text(
                json.dumps(cached, ensure_ascii=False))

    if args.json:
        print(json.dumps({label: str(path) for label, path in results}, ensure_ascii=False))
    else:
        for label, path in results:
            print(f"{label:<11}{path}")
