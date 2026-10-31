-- Meeting Transcriber.app: a Mac front end for the `transcribe` command.
-- Open the app to pick a recording, or drop audio or video files on its icon.
-- The run happens in a Terminal window, so progress and errors stay visible.
-- Build it with macos/build.sh.

property appTitle : "Meeting Transcriber"

on homePath()
	return POSIX path of (path to home folder)
end homePath

on configDir()
	return homePath() & ".config/meeting-transcriber/"
end configDir

on isConfigured()
	try
		do shell script "test -f " & quoted form of (configDir() & "config.yaml")
		return true
	on error
		return false
	end try
end isConfigured

-- Write a one-off .command file and open it in Terminal, so the user sees progress.
-- Opening a .command file needs no Automation permission.
on runInTerminal(body)
	set jobDir to homePath() & "Library/Caches/meeting-transcriber-app/"
	do shell script "mkdir -p " & quoted form of jobDir
	set jobFile to do shell script "mktemp " & quoted form of (jobDir & "job-XXXXXX") & " && true"
	set scriptText to "#!/bin/zsh" & linefeed & ¬
		"rm -f \"$0\"" & linefeed & ¬
		"export PATH=\"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH\"" & linefeed & ¬
		"clear" & linefeed & ¬
		"printf '\\e]0;Meeting Transcriber\\a'" & linefeed & ¬
		body & linefeed & ¬
		"echo" & linefeed & "read -rs '?Press Enter to close this window.'" & linefeed & "exit" & linefeed
	set cmdFile to jobFile & ".command"
	set fh to open for access (POSIX file cmdFile) with write permission
	set eof of fh to 0
	write scriptText to fh as «class utf8»
	close access fh
	do shell script "rm -f " & quoted form of jobFile & "; chmod +x " & quoted form of cmdFile & "; open -a Terminal " & quoted form of cmdFile
end runInTerminal

on runSetup()
	set body to "echo 'First-time setup for Meeting Transcriber.'" & linefeed & ¬
		"echo 'You need an ElevenLabs API key (speech to text) and a key for your LLM provider.'" & linefeed & ¬
		"echo 'Example: provider openrouter, model google/gemini-2.5-flash. Keys are typed hidden and stay in ~/.config/meeting-transcriber/.env'" & linefeed & ¬
		"echo" & linefeed & ¬
		"transcribe init && echo && transcribe doctor"
	runInTerminal(body)
end runSetup

on transcribeBody(fileArgs, speakerArg)
	return "out=$(mktemp)" & linefeed & ¬
		"if transcribe" & fileArgs & speakerArg & " --json > \"$out\"; then" & linefeed & ¬
		"  t=$(plutil -extract transcript raw -o - \"$out\")" & linefeed & ¬
		"  s=$(plutil -extract short raw -o - \"$out\" 2>/dev/null)" & linefeed & ¬
		"  echo; echo '──────── Short summary ────────'; [ -f \"$s\" ] && cat \"$s\"; echo" & linefeed & ¬
		"  echo; echo \"Saved in: $(dirname \"$t\")\"" & linefeed & ¬
		"  open \"$(dirname \"$t\")\"" & linefeed & ¬
		"  osascript -e 'display notification \"Transcript and summary are ready.\" with title \"Meeting Transcriber\" sound name \"Glass\"'" & linefeed & ¬
		"else" & linefeed & ¬
		"  echo; echo 'Transcription failed. See the message above.'" & linefeed & ¬
		"  osascript -e 'display notification \"Transcription failed. See the Terminal window.\" with title \"Meeting Transcriber\"'" & linefeed & ¬
		"fi" & linefeed & "rm -f \"$out\""
end transcribeBody

-- The Python that runs `transcribe`, read from its launcher, so the helpers below
-- see the same config and need nothing else installed.
on toolPython()
	return do shell script "export PATH=\"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH\"; head -1 \"$(command -v transcribe)\" | sed 's/^#!//'"
end toolPython

on splitText(t, delim)
	set AppleScript's text item delimiters to delim
	set parts to text items of t
	set AppleScript's text item delimiters to ""
	return parts
end splitText

on joinList(l, delim)
	set AppleScript's text item delimiters to delim
	set t to l as text
	set AppleScript's text item delimiters to ""
	return t
end joinList

-- Projects from config.yaml: a list of {key, title, people (a list of names)}.
on projectList()
	set code to "from meeting_transcriber.common import load_config" & linefeed & ¬
		"for k, p in (load_config().get('projects') or {}).items():" & linefeed & ¬
		"    print('\\t'.join([str(k), str(p.get('name') or k), '|'.join(map(str, p.get('speakers') or [])), '.']))"
	try
		set out to do shell script quoted form of toolPython() & " -I -c " & quoted form of code
	on error
		return {}
	end try
	set res to {}
	repeat with ln in paragraphs of out
		set parts to splitText(ln as text, tab)
		if (count of parts) is 4 then
			set people to {}
			if item 3 of parts is not "" then set people to splitText(item 3 of parts, "|")
			set end of res to {key:item 1 of parts, title:item 2 of parts, people:people}
		end if
	end repeat
	return res
end projectList

-- Read typed names the way people write them ("Alice & Bob and Sam: client"):
-- &, +, ;, / and "and" separate names as commas do. Returns {arg, lines, count}:
-- arg for -s, one display line per person, and a flag when something looks wrong.
on parseNames(t)
	set code to "import re, sys" & linefeed & ¬
		"text = sys.argv[1]" & linefeed & ¬
		"for sep in '&+;/\\n\\r': text = text.replace(sep, ',')" & linefeed & ¬
		"people = []" & linefeed & ¬
		"for item in text.split(','):" & linefeed & ¬
		"    name, _, role = item.partition(':')" & linefeed & ¬
		"    role = ' '.join(role.split())" & linefeed & ¬
		"    for n in re.split(r'\\s+(?:and|und)\\s+', name.strip(), flags=re.I):" & linefeed & ¬
		"        n = ' '.join(n.split())" & linefeed & ¬
		"        if n and n not in [p[0] for p in people]:" & linefeed & ¬
		"            odd = len(n.split()) > 3 or n[0].islower() or any(c.isdigit() for c in n)" & linefeed & ¬
		"            people.append((n, role, odd))" & linefeed & ¬
		"for n, role, odd in people:" & linefeed & ¬
		"    print('\\t'.join([n, role, 'odd' if odd else '', '.']))"
	set out to do shell script quoted form of toolPython() & " -I -c " & quoted form of code & " " & quoted form of t
	set args to {}
	set shown to {}
	set odd to false
	repeat with ln in paragraphs of out
		set parts to splitText(ln as text, tab)
		if (count of parts) is 4 then
			set {n, r, flag} to items 1 thru 3 of parts
			if r is "" then
				set end of args to n
				set line_ to "  •  " & n
			else
				set end of args to n & ": " & r
				set line_ to "  •  " & n & "  (role: " & r & ")"
			end if
			if flag is "odd" then
				set odd to true
				set line_ to line_ & "   ← is this a name?"
			end if
			set end of shown to line_
		end if
	end repeat
	return {arg:joinList(args, ", "), shownLines:shown, odd:odd}
end parseNames

-- Who was on the call: a project (tick its people) and any guests, then a check
-- of exactly whom the tool will look for, since each name caps the voices it splits.
-- Returns the flags for `transcribe`, or "" to work it all out.
-- The `transcribe` flags for a project key ("" for none), the project's people who
-- were not on the call, and the -s text for everyone else.
on buildFlags(projKey, dropped, guestsArg)
	set flags to ""
	if projKey is not "" then set flags to " -p " & quoted form of projKey
	if (count of dropped) > 0 then set flags to flags & " -d " & quoted form of joinList(dropped, ", ")
	if guestsArg is not "" then set flags to flags & " -s " & quoted form of guestsArg
	return flags
end buildFlags

on askPeople(fileCount)
	set projects to projectList()
	set proj to missing value
	if (count of projects) > 0 then
		set titles to {}
		repeat with p in projects
			set end of titles to title of p
		end repeat
		set end of titles to "No project"
		set pick to choose from list titles with title appTitle with prompt "Which project is this call for?" default items {item 1 of titles}
		if pick is false then error number -128
		repeat with p in projects
			if title of p is item 1 of pick then set proj to contents of p
		end repeat
	end if
	set guestsText to ""
	repeat
		set flags to ""
		set ticked to {}
		set dropped to {}
		if proj is not missing value and (count of people of proj) > 0 then
			set ticked to choose from list (people of proj) with title appTitle with prompt "Who was on the call? Untick anyone who wasn't." default items (people of proj) with multiple selections allowed and empty selection allowed
			if ticked is false then error number -128
			repeat with n in people of proj
				if ticked does not contain (contents of n) then set end of dropped to contents of n
			end repeat
		end if
		if proj is not missing value then
			set q to "Anyone else on the call? Names separated by commas." & return & "Add a role after a colon if it helps, e.g. Sam: client." & return & "Leave empty if no one else."
		else
			set q to "Who was on the call? Names separated by commas." & return & "Add a role after a colon if it helps, e.g. Sam: client." & return & "Leave empty to let it work out the names."
		end if
		set guestsText to text returned of (display dialog q default answer guestsText buttons {"Cancel", "Next"} default button 2 with title appTitle with icon note)
		set guests to parseNames(guestsText)
		if proj is missing value then
			set flags to buildFlags("", {}, arg of guests)
		else
			set flags to buildFlags(key of proj, dropped, arg of guests)
		end if

		set shown to {}
		repeat with n in ticked
			set end of shown to "  •  " & n
		end repeat
		set shown to shown & (shownLines of guests)
		set total to count of shown
		if total is 0 then
			set msg to "No names given: it will work out how many people spoke and try to name them from the conversation."
		else
			set msg to "It will look for " & total & " voice"
			if total > 1 then set msg to msg & "s"
			set msg to msg & ":" & return & joinList(shown, return) & return & return & "Each name is one voice, so list everyone who spoke and no one else."
		end if
		if proj is not missing value then set msg to "Project: " & (title of proj) & return & return & msg
		if odd of guests then set msg to msg & return & return & "Something above does not look like a name. Edit it if it was meant as a note."
		if fileCount > 1 then set msg to msg & return & return & "The " & fileCount & " files are joined into one call. Transcribe separate calls one at a time."
		set b to button returned of (display dialog msg buttons {"Cancel", "Edit", "Transcribe"} default button 3 with title appTitle with icon note)
		if b is "Transcribe" then return flags
	end repeat
end askPeople

on transcribeFiles(theFiles)
	if not isConfigured() then
		display dialog "Meeting Transcriber is not set up yet. It needs your ElevenLabs and LLM API keys first." buttons {"Cancel", "Set up now"} default button 2 with title appTitle with icon note
		runSetup()
		return
	end if
	set fileArgs to ""
	repeat with f in theFiles
		set fileArgs to fileArgs & " " & quoted form of (POSIX path of f)
	end repeat
	set flags to askPeople(count of theFiles)
	runInTerminal(transcribeBody(fileArgs, flags))
end transcribeFiles

on chooseAndTranscribe()
	set theFiles to choose file with prompt "Choose a recording (audio or video). Several files are joined into one call." of type {"public.audio", "public.movie", "public.audiovisual-content"} default location (path to downloads folder) with multiple selections allowed
	transcribeFiles(theFiles)
end chooseAndTranscribe

on moreMenu()
	set opts to {"Check setup", "Edit API keys", "Edit config (models, projects)", "Open meetings folder", "Record a call (needs BlackHole)", "Voice recognition: enroll voices", "Voice recognition: open clips folder", "Run first-time setup", "Update Meeting Transcriber"}
	set pick to choose from list opts with title appTitle with prompt "What would you like to do?" default items {"Check setup"}
	if pick is false then return
	set pick to item 1 of pick
	if pick is "Check setup" then
		runInTerminal("transcribe doctor")
	else if pick is "Edit API keys" then
		if not isConfigured() then return runSetup()
		do shell script "open -e " & quoted form of (configDir() & ".env")
	else if pick is "Edit config (models, projects)" then
		if not isConfigured() then return runSetup()
		do shell script "open -e " & quoted form of (configDir() & "config.yaml")
	else if pick is "Open meetings folder" then
		do shell script "mkdir -p ~/Meetings && open ~/Meetings"
	else if pick is "Record a call (needs BlackHole)" then
		runInTerminal("echo 'Recording. Press Ctrl-C to stop; the file goes to your inbox.'; echo 'Then open Meeting Transcriber and choose the recording.'; echo; transcribe record")
	else if pick is "Voice recognition: enroll voices" then
		runInTerminal("echo 'Building voiceprints from the clips in ~/.config/meeting-transcriber/voices'; echo; transcribe voices enroll && echo && transcribe voices check")
	else if pick is "Voice recognition: open clips folder" then
		do shell script "mkdir -p " & quoted form of (configDir() & "voices") & " && open " & quoted form of (configDir() & "voices")
	else if pick is "Run first-time setup" then
		runSetup()
	else if pick is "Update Meeting Transcriber" then
		runInTerminal("uv tool upgrade meeting-transcriber")
	end if
end moreMenu

on run
	if not isConfigured() then
		set r to button returned of (display dialog "Welcome to Meeting Transcriber." & return & return & "It turns a call recording into a transcript with named speakers plus a summary." & return & return & "First, set it up with your ElevenLabs and LLM API keys." buttons {"Quit", "Set up"} default button 2 with title appTitle with icon note)
		if r is "Set up" then runSetup()
		return
	end if
	set r to button returned of (display dialog "Turn a call recording into a transcript with named speakers and a summary." & return & return & "Tip: drop audio or video files on the app icon." buttons {"More…", "Quit", "Transcribe a recording…"} default button 3 cancel button 2 with title appTitle with icon note)
	if r is "Transcribe a recording…" then
		chooseAndTranscribe()
	else if r is "More…" then
		moreMenu()
	end if
end run

on open theFiles
	transcribeFiles(theFiles)
end open
