"""
Tests for the parts of the pipeline where bugs actually lived: parsing, turn
building, speaker naming, rendering, file naming, provider
requests, and one end-to-end run with the two network calls stubbed out.

No network, no API keys, no real config: run with `uv run pytest`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys

import pytest

from meeting_transcriber import common, llm, summarize
from meeting_transcriber import transcribe as tr

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


# ── parsing ──────────────────────────────────────────────────────────────────

def test_find_project_by_key_or_name():
    projects = {"personal": {"name": "Personal"}, "work": {"name": "Day Job"}}
    assert common.find_project(projects, "day job")[0] == "work"
    assert common.find_project(projects, "PERSONAL")[0] == "personal"
    with pytest.raises(common.Error):
        common.find_project(projects, "nope")


def test_parse_people_splits_names_and_roles():
    names, roles = tr.parse_people("Alice: designer, Bob, , Carol: CEO")
    assert names == ["Alice", "Bob", "Carol"]
    assert roles == {"Alice": "designer", "Carol": "CEO"}


@pytest.mark.parametrize("raw,slug", [
    ("Interview with Sam", "interview-with-sam"),
    ("Réunion avec  ACME!", "réunion-avec-acme"),
    ("  Weekly_sync  ", "weekly-sync"),
])
def test_slugify(raw, slug):
    assert common.slugify(raw) == slug


def test_load_env_handles_quotes_comments_and_existing_vars(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('A_KEY="quoted # not a comment"\nB_KEY=plain  # comment\n'
                   "C_KEY=\n# D_KEY=commented\nE_KEY=from-file\n")
    monkeypatch.setattr(common, "ENV", env)
    for k in ("A_KEY", "B_KEY", "C_KEY", "D_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("E_KEY", "from-shell")
    common.load_env()
    assert os.environ["A_KEY"] == "quoted # not a comment"
    assert os.environ["B_KEY"] == "plain"
    assert "C_KEY" not in os.environ and "D_KEY" not in os.environ
    assert os.environ["E_KEY"] == "from-shell"


# ── dates and cache keys ─────────────────────────────────────────────────────

def test_infer_date_takes_the_earliest_named_file(tmp_path):
    files = [tmp_path / "2026-07-08-b.ogg", tmp_path / "2026-07-05-a.ogg"]
    assert tr.infer_date(files) == "2026-07-05"


def test_infer_date_falls_back_to_mtime(tmp_path):
    f = tmp_path / "recording.ogg"
    f.write_bytes(b"x")
    stamp = dt.datetime(2020, 1, 15, 12, 0).timestamp()  # midday: no timezone edge
    os.utime(f, (stamp, stamp))
    assert tr.infer_date([f]) == "2020-01-15"


def test_fingerprint_is_stable_and_content_sensitive(tmp_path):
    a, b = tmp_path / "a.ogg", tmp_path / "b.ogg"
    a.write_bytes(b"one")
    b.write_bytes(b"two")
    assert tr.fingerprint([a, b]) == tr.fingerprint([a, b])
    assert tr.fingerprint([a, b]) != tr.fingerprint([b, a])
    b.write_bytes(b"three")
    assert tr.fingerprint([a, b]) != tr.fingerprint([a])


def test_collect_audio_takes_media_from_the_inbox_only(tmp_path):
    for name in ("a.m4a", "b.MP4", "notes.txt", "c.amr"):
        (tmp_path / name).write_bytes(b"x")
    assert [f.name for f in tr.collect_audio([], tmp_path)] == ["a.m4a", "b.MP4", "c.amr"]
    with pytest.raises(common.Error):
        tr.collect_audio([], tmp_path / "missing")


# ── audio ────────────────────────────────────────────────────────────────────

def tone(path, seconds=1, *extra):
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}", *extra, str(path)],
                   check=True)


@needs_ffmpeg
def test_prepare_audio_joins_any_mix_of_formats(tmp_path):
    files = [tmp_path / "a.wav", tmp_path / "b.flac", tmp_path / "c.mp3"]
    for f in files:
        tone(f)
    stereo = tmp_path / "d.m4a"
    tone(stereo, 1, "-ac", "2", "-ar", "44100")
    out = tr.prepare_audio([*files, stereo], tmp_path)
    assert out.suffix == ".ogg"
    assert 3.8 < tr.duration(out) < 4.3


@needs_ffmpeg
def test_prepare_audio_rejects_a_file_without_sound(tmp_path):
    bad = tmp_path / "notes.mp3"
    bad.write_text("not audio")
    with pytest.raises(common.Error, match="ffmpeg could not read"):
        tr.prepare_audio([bad], tmp_path)


# ── turns ────────────────────────────────────────────────────────────────────

def words(*items) -> list[dict]:
    return [{"text": t, "type": ty, "speaker_id": s} for t, ty, s in items]


def test_build_turns_groups_by_speaker_and_keeps_spacing():
    turns = tr.build_turns(words(
        ("Hello", "word", "speaker_0"),
        (" ", "spacing", "speaker_0"),
        ("Sam", "word", "speaker_0"),
        ("Hi", "word", "speaker_1"),
    ))
    assert turns == [{"speaker": "speaker_0", "text": "Hello Sam"},
                     {"speaker": "speaker_1", "text": "Hi"}]


def test_build_turns_defaults_a_missing_speaker_and_drops_empties():
    turns = tr.build_turns([
        {"text": " ", "type": "spacing"},            # before any turn: dropped
        {"text": "Ok", "type": "word"},              # no speaker_id: speaker_0
    ])
    assert turns == [{"speaker": "speaker_0", "text": "Ok"}]


# ── speaker naming ───────────────────────────────────────────────────────────

def test_parse_mapping_tolerates_fences_flat_values_and_gaps():
    raw = ('```json\n{"speaker_0": {"name": "Sam", "confidence": "HIGH"},'
           ' "speaker_1": "Carol"}\n```')
    out = tr.parse_mapping(raw, ["speaker_0", "speaker_1", "speaker_2"])
    assert out["speaker_0"] == {"name": "Sam", "confidence": "high", "evidence": ""}
    assert out["speaker_1"]["name"] == "Carol"
    assert out["speaker_2"]["name"] == "speaker_2"          # not labelled: kept as is
    with pytest.raises(common.Error):
        tr.parse_mapping("no json here", ["speaker_0"])


def test_without_a_roster_the_prompt_forbids_inventing_names(monkeypatch):
    seen = {}

    def fake(cfg, prompt, model=None):
        seen["prompt"] = prompt
        return "{}"

    monkeypatch.setattr(llm, "complete", fake)
    turns = [{"speaker": "speaker_0", "text": "hi"}, {"speaker": "speaker_1", "text": "yo"}]
    tr.map_speakers(turns, {"speakers": []}, {"model": "m"})
    assert "Never invent a name" in seen["prompt"]
    tr.map_speakers(turns, {"speakers": ["Sam", "Carol"]}, {"model": "m"})
    assert "strictly from the participant list" in seen["prompt"]


# ── rendering and file naming ────────────────────────────────────────────────

def test_speaker_label_marks_guesses_and_numbers_unnamed_labels():
    mapping = {"speaker_0": {"name": "Sam", "confidence": "high"},
               "speaker_1": {"name": "Carol", "confidence": "low"}}
    assert tr.speaker_label(mapping, "speaker_0") == "Sam"
    assert tr.speaker_label(mapping, "speaker_1") == "Carol?"
    assert tr.speaker_label(mapping, "speaker_9") == "Speaker 10"


def test_render_md_merges_consecutive_turns_of_one_speaker():
    mapping = {"speaker_0": {"name": "Sam", "confidence": "high"},
               "speaker_1": {"name": "Carol", "confidence": "high"}}
    md = tr.render_md([{"speaker": "speaker_0", "text": "One."},
                       {"speaker": "speaker_0", "text": "Two."},
                       {"speaker": "speaker_1", "text": "Three."}], mapping)
    assert md == "Sam: One. Two.\n\nCarol: Three.\n"


def test_frontmatter_quotes_names_safely():
    fm = tr.render_frontmatter("2026-08-09", "Call: part 2", "", ["Dr. Lee, MD", "Sam"])
    assert summarize.split_frontmatter(fm + "body")[0]["participants"] == ["Dr. Lee, MD", "Sam"]


def test_unique_path_walks_up_the_suffixes(tmp_path):
    p = tmp_path / "2026-08-09.md"
    assert common.unique_path(p) == p
    p.write_text("x")
    assert common.unique_path(p).name == "2026-08-09-2.md"
    (tmp_path / "2026-08-09-2.md").write_text("x")
    assert common.unique_path(p).name == "2026-08-09-3.md"


def test_build_keyterms_dedupes_and_keeps_speakers_first():
    settings = {"keyterms": ["SKU", "Sam", "dbt"]}
    assert tr.build_keyterms(settings, ["Sam", "Carol"]) == ["Sam", "Carol", "SKU", "dbt"]


def test_build_keyterms_caps_the_list(caplog):
    terms = tr.build_keyterms({"keyterms": [f"t{i}" for i in range(200)]}, [])
    assert len(terms) == tr.KEYTERM_CAP


# ── summary side ─────────────────────────────────────────────────────────────

def test_split_frontmatter_reads_the_yaml_block():
    fm, body = summarize.split_frontmatter("---\ntitle: Sync\ntags: [a, b]\n---\n## TL;DR\ntext\n")
    assert fm == {"title": "Sync", "tags": ["a", "b"]}
    assert body.startswith("## TL;DR")


@pytest.mark.parametrize("md", [
    "## TL;DR\nno frontmatter\n",
    "---\ntitle: [unclosed\n---\nbody\n",
    "---\n- a\n- b\n---\nbody\n",
    "---\nnever closed\n",
])
def test_split_frontmatter_survives_bad_input(md):
    fm, body = summarize.split_frontmatter(md)
    assert fm == {}
    assert body


def test_plainify_strips_markdown_for_chat_apps():
    out = summarize._plainify("```\n## Summary\n**Bold** text\n- item\n* other\n\n\n\ntail")
    assert "**" not in out and "#" not in out and "```" not in out
    assert "• item" in out and "• other" in out
    assert "\n\n\n" not in out
    assert out.endswith("\n")


def test_summary_language_defaults_to_the_conversations_own():
    assert "conversation" in summarize.language_name("auto")
    assert "conversation" in summarize.language_name(None)
    assert summarize.language_name("German") == "German"


def test_summary_lands_in_the_meeting_folder(tmp_path):
    nested = tmp_path / "2026-08-09" / "transcript.md"
    assert summarize.summary_target(nested) == (nested.parent, "summary")
    assert summarize.short_path(nested.parent / "summary.md").name == "summary-short.txt"


def test_flat_folders_keep_their_summaries_subdir(tmp_path):
    flat = tmp_path / "notes" / "2026-08-09-physio.md"
    assert summarize.summary_target(flat) == (tmp_path / "notes" / "summaries",
                                              "2026-08-09-physio")


def test_speakers_come_from_the_transcript_not_the_roster():
    md = ("---\ntype: transcript\nparticipants: [Alex]\n---\n\n"
          "Sam: Hi, e.g.: this\n\nAlex: Hello\n\nAnna?: yes\n\nSam: bye\n")
    assert summarize.speakers_in(md) == ["Sam", "Alex", "Anna"]


# ── meeting folders ──────────────────────────────────────────────────────────

@pytest.fixture
def meetings(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "CACHE", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    return tmp_path / "meetings"


def test_a_stopped_run_resumes_into_the_same_meeting(meetings):
    src = [meetings.parent / "a.ogg"]
    assert tr.resolve_meeting(meetings, "2026-08-09", False, src, "fp1") == \
        (meetings / "2026-08-09", "2026-08-09", False)
    assert tr.resolve_meeting(meetings, "2026-08-09", False, src, "fp1") == \
        (meetings / "2026-08-09", "2026-08-09", True)
    assert tr.resolve_meeting(meetings, "2026-08-09", False, src, "fp2")[0] == \
        meetings / "2026-08-09-2"


def test_an_archived_recording_goes_back_into_its_own_meeting(meetings):
    rec = meetings / "2026-08-09-2" / "recording.ogg"
    assert tr.resolve_meeting(meetings, "2020-01-01", False, [rec], "fp") == \
        (meetings / "2026-08-09-2", "2026-08-09-2", True)
    flat_rec = meetings / "recordings" / "2026-08-09-physio.ogg"
    assert tr.resolve_meeting(meetings, "2020-01-01", True, [flat_rec], "fp") == \
        (meetings, "2026-08-09-physio", True)


def test_flat_meetings_take_a_free_stem(meetings):
    meetings.mkdir()
    (meetings / "2026-08-09-physio.md").write_text("note")
    assert tr.resolve_meeting(meetings, "2026-08-09-physio", True,
                              [meetings.parent / "x.ogg"], "fp") == \
        (meetings, "2026-08-09-physio-2", False)


def test_inbox_files_are_moved_and_explicit_paths_copied(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    queued, given = inbox / "a.m4a", tmp_path / "b.webm"
    queued.write_bytes(b"audio")
    given.write_bytes(b"audio")
    assert tr.archive_meeting([queued], queued, tmp_path / "m1", "m1", False, inbox) == \
        tmp_path / "m1" / "recording.m4a"
    assert not queued.exists()
    tr.archive_meeting([given], given, tmp_path / "m2", "m2", False, inbox)
    assert given.exists()


def test_flat_projects_keep_filing_by_stem(tmp_path):
    src = tmp_path / "raw.ogg"
    src.write_bytes(b"audio")
    rec = tr.archive_meeting([src], src, tmp_path, "2026-08-09-physio", True, tmp_path / "in")
    out = tr.write_transcript("Sam: hi\n", tmp_path, "2026-08-09-physio", flat=True)
    assert rec == tmp_path / "recordings" / "2026-08-09-physio.ogg"
    assert out == tmp_path / "2026-08-09-physio.md"


def test_rearchiving_the_same_recording_is_a_no_op(tmp_path):
    mtg = tmp_path / "2026-08-09"
    mtg.mkdir()
    rec = mtg / "recording.ogg"
    rec.write_bytes(b"audio")
    assert tr.archive_meeting([rec], rec, mtg, "2026-08-09", False, tmp_path / "in") == rec
    assert sorted(q.name for q in mtg.iterdir()) == ["recording.ogg"]


# ── LLM providers ────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.is_success = 200 <= status < 300
        self.text = json.dumps(data)

    def json(self):
        return self._data


@pytest.fixture
def posts(monkeypatch):
    import httpx

    calls, replies = [], []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return replies.pop(0)

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    return calls, replies


@pytest.mark.parametrize("provider,url,env", [
    ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY"),
    ("fireworks", "https://api.fireworks.ai/inference/v1/chat/completions",
     "FIREWORKS_API_KEY"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
     "GEMINI_API_KEY"),
])
def test_openai_compatible_providers(posts, monkeypatch, provider, url, env):
    calls, replies = posts
    monkeypatch.setenv(env, "k")
    replies.append(FakeResponse(200, {"choices": [{"message": {"content": "hi"}}]}))
    assert llm.complete({"provider": provider, "model": "m"}, "prompt") == "hi"
    assert calls[0]["url"] == url
    assert calls[0]["headers"]["Authorization"] == "Bearer k"
    assert calls[0]["json"]["messages"] == [{"role": "user", "content": "prompt"}]


def test_custom_gateway_with_its_own_key_variable(posts, monkeypatch):
    calls, replies = posts
    monkeypatch.setenv("MY_GATEWAY", "secret")
    replies.append(FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}))
    cfg = {"provider": "openai-compatible", "model": "m", "api_key_env": "MY_GATEWAY",
           "base_url": "http://localhost:4000/v1/"}
    assert llm.complete(cfg, "p") == "ok"
    assert calls[0]["url"] == "http://localhost:4000/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer secret"


def test_anthropic_native_api(posts, monkeypatch):
    calls, replies = posts
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    replies.append(FakeResponse(200, {"content": [{"type": "text", "text": "a"},
                                                  {"type": "text", "text": "b"}]}))
    assert llm.complete({"provider": "anthropic", "model": "m"}, "p") == "ab"
    assert calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == "k"


def test_rate_limits_are_retried_and_other_errors_are_not(posts, monkeypatch):
    calls, replies = posts
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    replies += [FakeResponse(429, {}), FakeResponse(200, {"choices": [{"message": {"content": "x"}}]})]
    assert llm.complete({"provider": "openai", "model": "m"}, "p") == "x"
    replies.append(FakeResponse(401, {"error": "bad key"}))
    with pytest.raises(common.Error, match="HTTP 401"):
        llm.complete({"provider": "openai", "model": "m"}, "p")


def test_missing_settings_are_reported_clearly(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(common.Error, match="llm.provider and llm.model"):
        llm.complete({}, "p")
    with pytest.raises(common.Error, match="unknown llm.provider"):
        llm.complete({"provider": "nope", "model": "m"}, "p")
    with pytest.raises(common.Error, match="GROQ_API_KEY is not set"):
        llm.complete({"provider": "groq", "model": "m"}, "p")


# ── setup and the whole pipeline ─────────────────────────────────────────────

def cli(tmp_path, *argv):
    env = {**os.environ, "MEETING_TRANSCRIBER_HOME": str(tmp_path / "home"),
           "HOME": str(tmp_path)}
    for k in [k for k in env if k.endswith("_API_KEY")]:
        del env[k]
    return subprocess.run([sys.executable, "-c",
                           "from meeting_transcriber.cli import main; main()", *argv],
                          env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def test_init_writes_a_config_that_loads(tmp_path):
    r = cli(tmp_path, "init")
    assert r.returncode == 0, r.stderr
    home = tmp_path / "home"
    assert (home / "config.yaml").exists() and (home / ".env").exists()
    assert oct((home / ".env").stat().st_mode)[-3:] == "600"
    assert (tmp_path / "Meetings" / "inbox").is_dir()
    assert cli(tmp_path, "projects").stdout.startswith("No projects yet")


def test_init_flags_set_the_provider_without_prompts(tmp_path):
    r = cli(tmp_path, "init", "--provider", "openai-compatible", "--model", "my-model",
            "--base-url", "http://localhost:4000/v1")
    assert r.returncode == 0, r.stderr
    config = (tmp_path / "home" / "config.yaml").read_text()
    assert "  provider: openai-compatible\n  model: my-model\n" in config
    assert "  base_url: http://localhost:4000/v1" in config
    # a second run with flags updates the existing config in place
    cli(tmp_path, "init", "--provider", "groq", "--model", "other")
    assert "  provider: groq\n  model: other\n" in (tmp_path / "home" / "config.yaml").read_text()


def test_doctor_names_what_is_missing_and_fails(tmp_path):
    cli(tmp_path, "init")
    r = cli(tmp_path, "doctor", "--offline")
    assert r.returncode == 1
    assert "FAIL ElevenLabs key" in r.stdout and "fix: add ELEVENLABS_API_KEY" in r.stdout
    assert "FAIL LLM openrouter" in r.stdout


def test_dragged_paths_lose_quotes_and_escapes():
    assert str(tr.as_path(r"/a/My\ Call\ \(1\).m4a")) == "/a/My Call (1).m4a"
    assert str(tr.as_path("'/a b/c.m4a' ")) == "/a b/c.m4a"


def test_guide_picks_with_enter_and_numbers(tmp_path, monkeypatch):
    recs = [tmp_path / "new.m4a", tmp_path / "old.mp3"]
    for f in recs:
        f.write_bytes(b"x")
    monkeypatch.setattr(tr, "recent_recordings", lambda: recs)
    answers = iter(["", "2"])                      # Enter = newest file, project 2
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    args = tr.parser().parse_args([])
    tr.guide(args, {"projects": {"a": {}, "b": {"speakers": ["Sam"]}}})
    assert args.audio == [str(recs[0])] and args.project == "b" and args.speakers == ""

    answers = iter(["9", str(recs[1]), "Alice, Sam: client"])   # bad number, then a path
    args = tr.parser().parse_args([])
    tr.guide(args, {})
    assert args.audio == [str(recs[1])] and args.speakers == "Alice, Sam: client"


@needs_ffmpeg
def test_end_to_end_with_stubbed_services(tmp_path, monkeypatch, capsys):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    tone(inbox / "2026-08-09-call.mp3", 2)
    cfg = {**common.DEFAULTS, "llm": {"provider": "openai", "model": "m"},
           "output_dir": str(tmp_path / "out"), "inbox": str(inbox)}
    monkeypatch.setattr(tr, "load_config", lambda: cfg)
    monkeypatch.setattr(tr, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(tr, "scribe", lambda *a: [
        {"text": "Hi, I'm Sam.", "type": "word", "speaker_id": "speaker_0", "start": 0.0},
        {"text": "Hello.", "type": "word", "speaker_id": "speaker_1", "start": 1.0}])

    def fake_llm(cfg, prompt, model=None):
        if "labelling" in prompt:
            return '{"speaker_0": {"name": "Sam", "confidence": "high", "evidence": "x"}}'
        if "condense" in prompt:
            return "Summary\n\nThey said hello.\n"
        return "---\ntitle: Hello\nparticipants: [Sam]\n---\n## TL;DR\nHello.\n"

    monkeypatch.setattr(llm, "complete", fake_llm)
    tr.run(["--json"])

    mtg = tmp_path / "out" / "2026-08-09"
    assert (mtg / "transcript.md").read_text() == "Sam: Hi, I'm Sam.\n\nSpeaker 2: Hello.\n"
    assert (mtg / "summary.md").exists() and (mtg / "summary-short.txt").exists()
    assert (mtg / "recording.mp3").exists() and not any(inbox.iterdir())
    result = json.loads(capsys.readouterr().out)
    assert result == {"transcript": str(mtg / "transcript.md"),
                      "summary": str(mtg / "summary.md"),
                      "short": str(mtg / "summary-short.txt"),
                      "recording": str(mtg / "recording.mp3")}


@needs_ffmpeg
def test_cache_from_another_model_is_not_reused(tmp_path, monkeypatch):
    audio = tmp_path / "2026-08-09-call.mp3"
    tone(audio, 2)
    cfg = {**common.DEFAULTS, "llm": {"provider": "openai", "model": "m"},
           "output_dir": str(tmp_path / "out"), "transcription_model": "scribe_v3"}
    monkeypatch.setattr(tr, "load_config", lambda: cfg)
    monkeypatch.setattr(tr, "CACHE", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    # written before the model was recorded, so it was scribe_v2
    (tmp_path / "cache" / f"{tr.fingerprint([audio])}.scribe.json").write_text(
        json.dumps({"params": {"num_speakers": None}, "words": []}))
    with pytest.raises(common.Error, match="with scribe_v2.*--retranscribe"):
        tr.run([str(audio)])


def test_complete_uses_an_injected_call_and_sends_nothing():
    seen = []
    cfg = {"call": lambda prompt, model: seen.append((prompt, model)) or "ok", "model": "m"}
    assert llm.complete(cfg, "hi") == "ok"
    assert llm.complete(cfg, "yo", "other") == "ok"
    assert seen == [("hi", "m"), ("yo", "other")]
