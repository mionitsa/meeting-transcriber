"""The `transcribe` command: dispatch to subcommands, report errors cleanly."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import __version__, llm, record, summarize, transcribe
from .common import (CONFIG, ENV, HOME, PACKAGE, Error, apply_verbosity, find_project,
                     load_config, load_env, log, settings_for, setup_logging,
                     verbosity_flags)


# ── transcribe init ──────────────────────────────────────────────────────────

def _ask(question: str, secret: bool = False, required: bool = False) -> str:
    while True:
        answer = (getpass.getpass if secret else input)(f"{question}: ").strip()
        if answer or not required:
            return answer


def _set_env(text: str, var: str, value: str) -> str:
    """Fill in `VAR=` in the .env template, uncommenting it if needed."""
    line = f"{var}={value}"
    new, n = re.subn(rf"^#?\s*{re.escape(var)}=.*$", line, text, count=1, flags=re.M)
    return new if n else f"{text.rstrip()}\n{line}\n"


def path_hint() -> str | None:
    """How to get `transcribe` onto PATH, if it is not there yet."""
    if shutil.which("transcribe"):
        return None
    repo = PACKAGE.parents[1]
    source = str(repo) if (repo / "pyproject.toml").exists() else "meeting-transcriber"
    return ("`transcribe` is not on your PATH yet. Either install it as a command:\n"
            f"    uv tool install {source}\n"
            "    uv tool update-shell        # once, if ~/.local/bin is not on PATH\n"
            "or add an alias to ~/.zshrc or ~/.bashrc:\n"
            f"    alias transcribe='uv run --project {source} transcribe'")


def _set_llm(config: str, provider: str, model: str, base_url: str | None) -> str:
    config = re.sub(r"^  provider: .*$", f"  provider: {provider}", config, count=1, flags=re.M)
    config = re.sub(r"^  model: .*$", f"  model: {model}", config, count=1, flags=re.M)
    if base_url:
        config = re.sub(r"^  #? ?base_url: .*$", f"  base_url: {base_url}", config,
                        count=1, flags=re.M)
    return config


def init(argv: list[str]) -> None:
    providers = [*llm.PROVIDERS, *llm.NATIVE]
    ap = argparse.ArgumentParser(
        prog="transcribe init", parents=[verbosity_flags()],
        description=f"Create the config and secrets in {HOME}. Asks for anything not "
                    "given as a flag when run in a terminal.",
        epilog="for scripts and AI agents:\n"
               "  transcribe init --provider openrouter --model google/gemini-2.5-flash\n"
               f"  then write the API keys into {ENV}",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=providers, metavar="NAME",
                    help=f"LLM provider: {', '.join(providers)}")
    ap.add_argument("--model", help="model id, as the provider names it")
    ap.add_argument("--base-url", help="endpoint, for openai-compatible gateways")
    args = ap.parse_args(argv)
    apply_verbosity(args)
    HOME.mkdir(parents=True, exist_ok=True)
    interactive = sys.stdin.isatty()
    exists = CONFIG.exists()
    config = CONFIG.read_text() if exists else (PACKAGE / "config.example.yaml").read_text()
    env = ENV.read_text() if ENV.exists() else (PACKAGE / "env.example").read_text()
    provider, model = args.provider, args.model

    if exists and not (provider or model):
        log.info("Keeping the existing config: %s", CONFIG)
    else:
        if interactive and not provider:
            print(f"LLM providers: {', '.join(providers)}")
            while provider not in providers:
                provider = _ask("Provider [openrouter]").lower() or "openrouter"
        if interactive and not model:
            model = _ask("Model id, as your provider names it "
                         "(e.g. google/gemini-2.5-flash on openrouter)", required=True)
        base_url = args.base_url
        if interactive and provider == "openai-compatible" and not base_url:
            base_url = _ask("Base URL of the gateway (e.g. http://localhost:4000/v1)",
                            required=True)
        if provider and model:
            config = _set_llm(config, provider, model, base_url)
        elif provider or model:
            raise Error("pass both --provider and --model")
        if interactive and not ENV.exists():
            print("Keys are written to the .env file only. Leave empty to fill in later.")
            if key := _ask("ElevenLabs API key", secret=True):
                env = _set_env(env, "ELEVENLABS_API_KEY", key)
            if provider == "vertex":
                if keyfile := _ask("Path to a service account key file (.json)"):
                    env = _set_env(env, "GOOGLE_APPLICATION_CREDENTIALS",
                                   str(Path(keyfile).expanduser().resolve()))
            elif var := llm.key_env(provider):    # None for ollama
                if key := _ask(var, secret=True):
                    env = _set_env(env, var, key)
        CONFIG.write_text(config)
        log.info("%s %s", "Updated" if exists else "Created", CONFIG)

    if not ENV.exists():
        ENV.write_text(env)
        ENV.chmod(0o600)
        log.info("Created %s", ENV)
    load_env()
    inbox = Path(settings_for(load_config(), None)["inbox"]).expanduser()
    inbox.mkdir(parents=True, exist_ok=True)

    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg is not installed (macOS: brew install ffmpeg, "
                    "Debian/Ubuntu: sudo apt install ffmpeg)")
    if hint := path_hint():
        log.warning("%s", hint)
    print(f"\n{'config':<9}{CONFIG}\n{'secrets':<9}{ENV}\n{'inbox':<9}{inbox}\n")
    print("Next: put any missing keys in the secrets file, then run\n"
          "    transcribe doctor")


# ── transcribe doctor ────────────────────────────────────────────────────────

def doctor(argv: list[str]) -> None:
    """Check everything a run needs, with a fix for each failure. Exit 1 if
    something required is missing, so scripts and agents can loop on it."""
    ap = argparse.ArgumentParser(prog="transcribe doctor", parents=[verbosity_flags()],
                                 description="Check the setup. Sends one tiny request "
                                             "to the LLM (skip with --offline).")
    ap.add_argument("--offline", action="store_true", help="do not contact the LLM")
    args = ap.parse_args(argv)
    apply_verbosity(args)
    failed = False

    def report(ok: bool | None, what: str, fix: str = "") -> None:
        nonlocal failed
        failed |= ok is False
        mark = {True: "ok  ", False: "FAIL", None: "--  "}[ok]
        print(f"{mark} {what}" + (f"\n       fix: {fix}" if fix and ok is False else
                                  f"  ({fix})" if fix and ok is None else ""))

    report(bool(shutil.which("ffmpeg")), "ffmpeg",
           "brew install ffmpeg (macOS) or sudo apt install ffmpeg (Linux)")
    report(path_hint() is None, "transcribe is on PATH",
           "uv tool install git+https://github.com/mionitsa/meeting-transcriber, "
           "then uv tool update-shell")
    if not CONFIG.exists():
        report(False, f"config {CONFIG}", "transcribe init")
        sys.exit(1)
    cfg = load_config()
    report(True, f"config {CONFIG}")
    report(bool(os.environ.get("ELEVENLABS_API_KEY")), "ElevenLabs key (ELEVENLABS_API_KEY)",
           f"add ELEVENLABS_API_KEY=... to {ENV}")

    lc = cfg.get("llm") or {}
    provider, model = lc.get("provider"), lc.get("model")
    name = f"LLM {provider} / {model}"
    var = lc.get("api_key_env") or llm.key_env(provider)
    if not (provider and model):
        report(False, "LLM provider and model", "transcribe init --provider ... --model ...")
    elif provider == "vertex" and not (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
                                       or shutil.which("gcloud")):
        report(False, f"{name}: credentials",
               f"add GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json to {ENV}")
    elif var and not os.environ.get(var):
        report(False, f"{name}: key ({var})", f"add {var}=... to {ENV}")
    elif args.offline:
        report(None, name, "not contacted")
    else:
        started = time.monotonic()
        try:
            llm.complete(lc, "Reply with the single word OK.")
            report(True, f"{name} replied in {time.monotonic() - started:.1f}s")
        except Error as e:
            report(False, name, str(e).splitlines()[0][:300])

    if failed:
        sys.exit(1)
    print("\nReady: transcribe path/to/recording.m4a")


# ── transcribe projects ──────────────────────────────────────────────────────

def projects(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="transcribe projects", parents=[verbosity_flags()],
                                 description="List the projects in config.yaml, or show one.")
    ap.add_argument("name", nargs="?", help="show this project's settings")
    args = ap.parse_args(argv)
    apply_verbosity(args)
    cfg = load_config()
    all_projects = cfg.get("projects") or {}
    if not args.name:
        if not all_projects:
            print(f"No projects yet. Add them under `projects:` in {CONFIG}")
        for key, p in all_projects.items():
            print(f"{key:<14}{p.get('name', ''):<18}{', '.join(p.get('speakers') or [])}")
        return
    key, p = find_project(all_projects, args.name)
    s = settings_for(cfg, p)
    roles = s.get("roles") or {}
    print(f"{key}: {s['name']}\n"
          f"  output:   {s['output_dir']}{' (flat)' if s.get('flat') else ''}\n"
          f"  summary:  {'on' if s['summary'] else 'off'}, "
          f"language {s['summary_language']}\n"
          f"  speakers:")
    for n in s.get("speakers") or []:
        print(f"    {n}" + (f": {roles[n]}" if n in roles else ""))
    print(f"  context:  {' '.join((s.get('context') or '').split())}\n"
          f"  keyterms: {', '.join(map(str, s.get('keyterms') or []))}")


COMMANDS = {"init": init, "doctor": doctor, "projects": projects, "summarize": summarize.run,
            "record": record.run}


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    setup_logging()
    load_env()
    try:
        if argv and argv[0] in ("-V", "--version"):
            print(f"meeting-transcriber {__version__}")
        elif argv and argv[0] in COMMANDS:
            COMMANDS[argv[0]](argv[1:])
        else:
            transcribe.run(argv)
    except Error as e:
        log.error("%s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        log.error("interrupted")
        sys.exit(130)
    except EOFError:      # Ctrl-D at a prompt
        print(file=sys.stderr)
        sys.exit(1)
