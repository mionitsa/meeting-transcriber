# Meeting Transcriber.app

A small Mac app around the `transcribe` command, for people who would rather not use
the terminal for every call.

- **Open it** to pick a recording, or **drop audio or video files** on its icon or Dock
  tile. Several files are joined in order, as with the command.
- It asks who was on the call. With projects in `config.yaml`, pick one and untick its
  speakers who were not there (they go to `-d`), then add any guests. `&`, `+`, `;` and
  "and" separate names as commas do. Before running, it shows exactly whom it will look
  for and flags anything that does not look like a name, since each name is one voice
  and the count caps how many voices speech recognition separates.
- It then runs `transcribe` in a Terminal window, so progress and errors stay visible.
- When it is done, it prints the short summary, opens the meeting folder and posts a
  notification.
- On first launch, with no config yet, it runs `transcribe init` and `transcribe doctor`
  instead. Keys are typed into the terminal, never into a dialog.
- **More…** checks the setup, opens the keys or `config.yaml` in TextEdit, opens
  `~/Meetings`, records a call (needs BlackHole), enrolls voices for recognition by
  voice (needs the `voice` extra) or upgrades the tool.

## Build

Install the command first (see the main README), then:

```bash
macos/build.sh --install
```

That builds `macos/build/Meeting Transcriber.app` and copies it to `/Applications`.
Without `--install` it only builds. It uses `osacompile`, `sips`, `iconutil` and
`codesign`, which ship with macOS, and `swift` from the Xcode command line tools to
draw the icon (without it the app keeps the default droplet icon).

## How it works

The app is an AppleScript droplet ([MeetingTranscriber.applescript](MeetingTranscriber.applescript)).
For each run it writes a one-off `.command` file to
`~/Library/Caches/meeting-transcriber-app/` and opens it with Terminal. The file
deletes itself as it starts. Opening a `.command` file needs no Automation permission,
unlike scripting Terminal directly. The run uses `transcribe ... --json` and reads the
result paths with `plutil`, so nothing beyond macOS and the CLI is needed. The project list
and the name check run in the Python that runs `transcribe` (read from its launcher),
so they see the same config.

The app is signed ad hoc, which is enough for an app you build on your own Mac. A copy
downloaded from the internet would need a Developer ID signature and notarisation.
