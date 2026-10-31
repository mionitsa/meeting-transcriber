# meeting-transcriber

Turn a recording of a call into a transcript where every line is attributed to a
named person, plus a summary of what was decided and who does what next.

```text
$ transcribe 2026-08-04-sync.m4a -s "Alice, Bob, Sam: client"
...
transcript ~/Meetings/2026-08-04/transcript.md
summary    ~/Meetings/2026-08-04/summary.md
short      ~/Meetings/2026-08-04/summary-short.txt
recording  ~/Meetings/2026-08-04/recording.m4a
```

```markdown
Alice: Let's start with the dashboard numbers. Sam, did your team get the export?

Sam: We did, but the retention column is empty for March.

Bob?: That's the backfill job, I'll rerun it today.
```

- **Any language.** Speech recognition detects it, and summaries are written in the
  language of the conversation (or one you choose).
- **Any file.** Audio or video in any format ffmpeg reads: m4a, mp3, wav, ogg, opus,
  flac, aac, amr, wma, mp4, mov, webm, mkv, and so on. Several parts are joined in order.
- **Any LLM.** OpenRouter, OpenAI, Anthropic, Gemini, Vertex AI, Fireworks, Together,
  Groq, DeepSeek, Mistral, xAI, a local Ollama, or any OpenAI-compatible gateway.
- **Honest speaker names.** A guess is marked `Name?` instead of being presented as fact,
  and optional voiceprints recognise regular speakers by their voice.
- **The words are never rewritten.** The LLM only decides which voice belongs to whom;
  the transcript text is exactly what speech recognition heard.

## How it works

```text
 audio or video file(s)
        |
 ffmpeg: decode and join into one mono track
        |
 ElevenLabs Scribe: words, timestamps, anonymous voices (speaker_0, speaker_1, ...)
        |
 voice ID (optional): compare each voice with enrolled voiceprints
        |-- confident match ---------> name pinned
        |-- unsure / unknown voice ---> passed on, with the scores
        |
 LLM: names the remaining voices from the conversation
        |
 transcript.md -> summary.md + summary-short.txt -> recording filed alongside
```

## Get started with your AI assistant

Paste this into Claude Code, Codex, Cursor or any coding assistant that can run
commands on your machine:

```text
Set up meeting-transcriber for me by following
https://github.com/mionitsa/meeting-transcriber/blob/main/AGENTS.md
Ask me which LLM provider I use and for my API keys. When `transcribe doctor`
passes, transcribe this recording as a test: <path to a recording>
```

It installs the tool, writes the config, puts your keys where they belong, checks the
setup and runs a first transcription. You need an
[ElevenLabs API key](https://elevenlabs.io/app/settings/api-keys) for speech to text,
and a key for whichever LLM provider you use.

Inside a clone of this repo, an assistant picks up [AGENTS.md](AGENTS.md) on its own:
just ask it to set the tool up.

## Set up by hand

You need [`uv`](https://docs.astral.sh/uv/) and `ffmpeg` (macOS: `brew install uv
ffmpeg`; Linux: `sudo apt install ffmpeg` and the
[uv installer](https://docs.astral.sh/uv/getting-started/installation/)). Then:

```bash
uv tool install git+https://github.com/mionitsa/meeting-transcriber
transcribe init      # asks for the provider, the model and your keys
transcribe doctor    # checks everything, and says how to fix what is missing
```

`uv tool install` puts `transcribe` on your PATH, so there is no alias to set up. If
your shell says `command not found`, run `uv tool update-shell` once and open a new
terminal. Optional extras go in the same command:

| Extra | For | Install |
|---|---|---|
| `vertex` | Google Vertex AI as the LLM | `uv tool install "meeting-transcriber[vertex] @ git+https://github.com/mionitsa/meeting-transcriber"` |
| `voice` | recognising speakers by voice (PyTorch, a large download) | `uv tool install "meeting-transcriber[voice] @ git+https://github.com/mionitsa/meeting-transcriber"` |

From a clone, `uv tool install -e .` installs your checkout.

Config lives in `~/.config/meeting-transcriber/`: `config.yaml` (provider, model,
folders, projects) and `.env` (keys, readable by you only). Set
`MEETING_TRANSCRIBER_HOME` to keep it elsewhere.

## Use

Type `transcribe` on its own and it lists your recent recordings (Downloads, Desktop,
Zoom, Voice Memos) and asks three quick questions. Enter takes the default every time,
and you can drag a file from Finder instead of typing a number:

```text
$ transcribe
Which recording? (a number, or drag a file in; Enter = 1, q = quit)
  1  2026-08-04 14:02    9.7 MB  ~/Downloads/weekly-sync.m4a
  2  2026-08-03 10:15   41.2 MB  ~/Documents/Zoom/2026-08-03 Client call/audio.m4a
> 
Who was on the call? (e.g. Alice, Sam: client; Enter = work it out) > Alice, Sam
```

Or pass everything at once:

```bash
transcribe call.m4a -s "Alice, Sam: client"     # who was on the call helps a lot
transcribe part1.mp3 part2.mp3 -n "Interview with Sam"
transcribe zoom.mp4 -l English -o .             # English summary, folder in .
transcribe -p work standup.m4a -d Carol         # a project, minus who was away
```

| Flag | |
|---|---|
| `-s, --speakers` | who was on the call, with optional roles: `"Alice, Sam: client"` |
| `-p, --project` | a project from `config.yaml` (regular speakers, context, folder) |
| `-d, --drop-speakers` | project regulars who were not on this call |
| `-n, --name` | what it is; names the folder `<date>-<name>` |
| `-l, --language` | language of the summaries (default: the conversation's) |
| `-o, --output` | where the meeting folder goes (default `~/Meetings`) |
| `--no-summary` | transcript only |
| `--json` | results as JSON on stdout, for scripts and agents |
| `-v`, `-q` | more detail, or warnings only |

Each recording gets its own folder:

```text
~/Meetings/2026-08-04-interview-with-sam/
  transcript.md        Name: text, one paragraph per turn
  summary.md           YAML frontmatter, TL;DR, decisions, next steps, open questions...
  summary-short.txt    plain text for a chat message (also printed at the end)
  recording.m4a        the original file (moved from the inbox, copied otherwise)
```

The transcription is cached by the audio's content: running the same command again,
after an interrupted run or to redo the summary, does not pay ElevenLabs twice. Files
dropped into `~/Meetings/inbox` are picked up when you run `transcribe` with no file.

Other commands:

```bash
transcribe doctor                                           # check the setup
transcribe summarize ~/Meetings/2026-08-04/transcript.md   # redo the summary
transcribe projects                                         # list projects
transcribe voices enroll                                    # build voiceprints
transcribe record                                           # record a call (macOS)
```

## LLM providers

Set `llm.provider` and `llm.model` in `config.yaml`, and the matching key in `.env`.
The model id is whatever the provider calls it; check their model list.

| provider | key in `.env` | notes |
|---|---|---|
| `openrouter` | `OPENROUTER_API_KEY` | one key for most models |
| `openai` | `OPENAI_API_KEY` | |
| `anthropic` | `ANTHROPIC_API_KEY` | |
| `gemini` | `GEMINI_API_KEY` | Google AI Studio key |
| `vertex` | `GOOGLE_APPLICATION_CREDENTIALS` | path to a service account key; needs the `vertex` extra |
| `fireworks` | `FIREWORKS_API_KEY` | |
| `together` | `TOGETHER_API_KEY` | |
| `groq` | `GROQ_API_KEY` | |
| `deepseek` | `DEEPSEEK_API_KEY` | |
| `mistral` | `MISTRAL_API_KEY` | |
| `xai` | `XAI_API_KEY` | |
| `ollama` | none | local models at `http://localhost:11434` |
| `openai-compatible` | `LLM_API_KEY` | LiteLLM, vLLM, LM Studio, a company gateway: set `base_url` |

```yaml
llm:
  provider: openai-compatible
  model: my-model
  base_url: https://llm.mycompany.com/v1
  api_key_env: MYCOMPANY_LLM_KEY     # optional: read the key from another variable
```

Rate limits and server errors are retried with backoff. A long transcript needs a model
with a large context window: a one-hour call is roughly 9,000 words.

## Projects

Recurring calls work better with a project: the list of regular speakers, their roles,
what the calls are about, and words speech recognition tends to get wrong.

```yaml
projects:
  work:
    name: Work
    output_dir: ~/Meetings/work
    speakers: [Alice, Bob, Carol]
    roles:
      Alice: team lead, runs the meeting
    context: Weekly syncs of a small analytics team.
    keyterms: [dbt, Airflow, BigQuery]
```

```bash
transcribe -p work standup.m4a --drop-speakers Carol --speakers "Dan: new hire"
```

A project can also set `summary`, `summary_language`, `flat: true` (write `<date>.md`
straight into a notes folder such as an Obsidian vault) and `frontmatter: true` (mark
transcripts apart from hand-written notes). See the comments in the generated
`config.yaml`.

## Recognising speakers by voice

Speech recognition separates voices well but does not know whose they are, and naming
them from the content fails when nobody says a name. With the `voice` extra installed,
record a short solo clip of each regular speaker (30 to 60 seconds of normal speech is
plenty) and put it in `~/.config/meeting-transcriber/voices/`, named
`<person>-<language>.<ext>`, for example `alice-en.m4a`. Then:

```bash
transcribe voices enroll   # build voiceprints from the clips
transcribe voices check    # similarity matrix: same person vs different people
```

From then on every label's voice is compared with the voiceprints of the expected
speakers (everyone enrolled, when no speaker list is given). A name is pinned only when
the match is strong, clearly ahead of the runner-up, and consistent across the label's
own audio. Weaker matches go to the LLM along with the scores, and a score below 0.30
counts as evidence that the voice is *not* that person, which the LLM is not allowed to
overrule. When one label turns out to hold two people, the parts that can be recognised
are split off.

Use clips from the same kind of audio as your calls, and enrol people who switch
languages once per language: matching across languages is noticeably weaker.

The thresholds were tuned on 15 real meetings with 49 speaker labels: 40 were named by
voice (scores 0.48 to 0.88, versus 0.08 to 0.27 for people with no voiceprint), the
other 9 were genuinely ambiguous and went to the LLM. In a language that marks gender in
past-tense verbs, 33 of the 40 had enough first-person speech to cross-check, and none
contradicted the voice match. That is one person's meetings and microphone, with no
hand-labelled ground truth, so treat the numbers as a starting point.

## Mac app

`macos/build.sh --install` builds Meeting Transcriber.app into `/Applications`. Open it
to pick a recording, or drop audio or video files on its icon: it asks who was on the
call, runs `transcribe` in a Terminal window and opens the meeting folder when it is
done. See [macos/README.md](macos/README.md).

## Recording calls on macOS

`transcribe record` captures the call audio (through
[BlackHole](https://github.com/ExistentialAudio/BlackHole)) and your microphone, mixes
them into one file in the inbox, and stops on Ctrl-C. One-time setup: `brew install
blackhole-2ch`, then in Audio MIDI Setup create a Multi-Output Device with both
BlackHole 2ch and your speakers or headphones, and select it as the sound output during
calls. `transcribe record --help` has the details. On other systems, record with any app
and pass the file.

## Privacy

The audio is sent to ElevenLabs, and the transcript text to your LLM provider. Use
`ollama` or your own gateway to keep the text on infrastructure you control. Config,
voiceprints and the cache stay on your machine (`~/.config/meeting-transcriber`,
`~/.cache/meeting-transcriber`).

## Limitations

- Speech to text is ElevenLabs only.
- Voice ID recognises enrolled people only. Two unenrolled guests under one label stay
  merged; a dedicated diarizer would be the proper fix.
- Speaker names are only as good as the clues: with no speaker list, no voiceprints and
  nobody saying a name, voices stay `Speaker 1`, `Speaker 2`.

## Development

```bash
git clone https://github.com/mionitsa/meeting-transcriber && cd meeting-transcriber
uv run pytest            # no network, no keys needed; ffmpeg for the audio tests
uv run transcribe --help
```

See [AGENTS.md](AGENTS.md) for the conventions.

```text
src/meeting_transcriber/
  cli.py          the `transcribe` command, init, projects
  transcribe.py   the pipeline: audio, Scribe, speaker naming, rendering, filing
  summarize.py    long and short summaries
  llm.py          LLM providers
  voiceid.py      voiceprint enrolment and matching
  record.py       macOS recorder
  common.py       paths, config, logging
tests/
```

## License

MIT, see [LICENSE](LICENSE).
