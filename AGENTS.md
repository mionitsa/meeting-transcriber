# Instructions for AI assistants

meeting-transcriber is a CLI, `transcribe`, that turns a recording into a Markdown
transcript with named speakers plus a summary. ElevenLabs does speech to text; an LLM
of the user's choice names the speakers and writes the summary.

## Setting it up for a user

Go through these in order, and run `transcribe doctor` at the end. Ask the user only for
what you cannot find out yourself.

1. **Prerequisites.** Check `uv --version` and `ffmpeg -version`. If one is missing, ask
   before installing it: `brew install uv ffmpeg` on macOS; on Linux,
   `sudo apt install ffmpeg` and `curl -LsSf https://astral.sh/uv/install.sh | sh`.

2. **Install.** Add `[vertex]` for Google Vertex AI:

   ```bash
   uv tool install git+https://github.com/mionitsa/meeting-transcriber
   # or, with Vertex AI support:
   uv tool install "meeting-transcriber[vertex] @ git+https://github.com/mionitsa/meeting-transcriber"
   ```

   From a clone of this repo: `uv tool install -e .` (or `-e ".[vertex]"`). If
   `transcribe` is then not found, run `uv tool update-shell` and use the full path
   `~/.local/bin/transcribe` for the rest of the session.

3. **Shadowing.** Run `type transcribe` (zsh/bash). If it reports an alias or a shell
   function, an older setup is overriding the command. Tell the user and offer to
   rename it in their shell rc file; until then call `command transcribe`.

4. **Choose the LLM.** Ask which provider the user has an account with, then pick a
   current model from that provider's own list (do not guess an id from memory; check
   their docs or model listing). If they have no preference, OpenRouter with a fast
   Gemini or Claude model is a good default: one key, many models.

   | provider | key variable in `.env` |
   |---|---|
   | `openrouter` | `OPENROUTER_API_KEY` |
   | `openai` | `OPENAI_API_KEY` |
   | `anthropic` | `ANTHROPIC_API_KEY` |
   | `gemini` | `GEMINI_API_KEY` (Google AI Studio) |
   | `vertex` | `GOOGLE_APPLICATION_CREDENTIALS` = path to a service account key `.json` |
   | `fireworks`, `together`, `groq`, `deepseek`, `mistral`, `xai` | `<NAME>_API_KEY` |
   | `ollama` | none (local) |
   | `openai-compatible` | `LLM_API_KEY`, plus `--base-url` |

5. **Write the config** (no prompts when flags are given):

   ```bash
   transcribe init --provider openrouter --model <model-id>
   ```

6. **Keys.** Ask the user for their ElevenLabs API key
   (https://elevenlabs.io/app/settings/api-keys, needs Speech to Text access) and the
   LLM key. Write them into `~/.config/meeting-transcriber/.env` as `NAME=value` lines,
   replacing the empty `NAME=` lines. Never put keys in `config.yaml`, on a command
   line or in the chat back to the user, and keep the file mode `600`.

7. **Check.** `transcribe doctor` prints `ok` or `FAIL` per item with a `fix:` line.
   Fix each `FAIL` and run it again until it ends with `Ready`.

8. **Test run.** Ask for a recording (any audio or video format) and who was on it:

   ```bash
   transcribe <file> -s "Name One, Name Two" --json
   ```

   `--json` prints `{"transcript": ..., "summary": ..., "short": ..., "recording": ...}`
   on stdout; progress goes to stderr. Show the user the short summary
   (`summary-short.txt`) and where the folder is.

9. **Projects (optional).** If the user has recurring calls, offer to add a project to
   `~/.config/meeting-transcriber/config.yaml`: `name`, `speakers`, `roles`, `context`,
   `keyterms` and optionally `output_dir`. The commented example in that file shows the
   shape. Then `transcribe -p <key> <file>`.

## Running it

```bash
transcribe <files...> [-p PROJECT] [-s "Alice, Sam: client"] [-d "Bob"] [-n "Title"]
           [-l LANGUAGE] [-o DIR] [--no-summary] [--json] [--retranscribe] [-v|-q]
transcribe summarize <transcript.md> [-p PROJECT] [--language LANG]
transcribe projects [NAME]
```

Speakers named `Name?` in a transcript were guessed with low confidence; `Speaker 2`
means nobody could tell. Re-running the same file does not charge ElevenLabs again.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `transcribe: command not found` | `uv tool update-shell`, open a new shell, or use `~/.local/bin/transcribe` |
| Old usage text (`--add-speakers`, `transcribe.py`) | a shell alias or function shadows the command, see step 3 |
| `no config at ...` | `transcribe init` |
| `... is not set. Put it in the .env file` | add that variable to `~/.config/meeting-transcriber/.env` |
| HTTP 404 or "model not found" | wrong model id for that provider; check their model list |
| Vertex `403 PERMISSION_DENIED` | the credentials' account has no access to the project; use a service account key from that project |
| `transcribed before with a different number of speakers` | add `--retranscribe` (charges ElevenLabs again) |
| `transcribed before with scribe_v2, the config now says ...` | add `--retranscribe` (charges ElevenLabs again) |
| summary cut off | raise `llm.max_tokens` in `config.yaml` |

## Working on the code

- `uv run pytest` runs the tests: no network or keys, ffmpeg needed for the audio tests.
- Layout: `src/meeting_transcriber/` (`cli.py` commands, `transcribe.py` pipeline,
  `summarize.py`, `llm.py` providers, `record.py`, `common.py`).
- Conventions: progress and warnings go through `log` (stderr); results go to stdout
  with `print`. Failures the user can act on raise `common.Error` with the fix in the
  message; `cli.main` prints it as `error: ...` and exits 1.
- The LLM never edits transcript text, it only maps speaker labels to names. Keep it
  that way.
