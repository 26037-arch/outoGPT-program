# OutoGPT

OutoGPT creates and continues ChatGPT conversations through the web interface and
records controller state locally. The repository contains:

- `CLI-gpt`: Playwright-based ChatGPT page actions and the Chrome/CDP lifecycle.
- `controller`: conversation IDs, operation states, SQLite persistence, and a
  machine-readable CLI.
- `extension`: an unpacked Chrome extension that archives conversations as Markdown.

## Browser architecture

OutoGPT does not use Playwright's bundled Chromium for normal operation.

It starts the normal installed Google Chrome executable as a separate subprocess,
using the persistent profile at `CLI-gpt/data/browser-profile/`, then connects
Playwright to that browser with Chrome DevTools Protocol (CDP):

```text
Controller -> CLI-gpt -> Playwright -> connect_over_cdp()
                                      -> installed Google Chrome
```

Chrome is started with the following essential arguments:

```text
chrome.exe
  --remote-debugging-address=127.0.0.1
  --remote-debugging-port=9222
  --user-data-dir=<repository>/CLI-gpt/data/browser-profile
```

The fixed debugging endpoint is loopback-only. OutoGPT refuses to attach when port
9222 is occupied but does not match its recorded dedicated Chrome session. It never
attempts to take over a daily-use Chrome profile.

## Install

Python 3.11 or newer and Google Chrome are required.

```powershell
python -m pip install -e ./CLI-gpt -e ./controller
```

Playwright remains the automation client. `playwright install chromium` is not
required for normal execution. If Chrome is installed in a nonstandard location,
set `OUTOGPT_CHROME_PATH` to its executable.

## First-run setup

Run:

```powershell
outogpt setup
outogpt setup --archive-root "D:\OneDrive\ChatGPT archive"
```

This opens ordinary Google Chrome with the dedicated OutoGPT profile. Before you
press Enter in the terminal, OutoGPT does not attach Playwright, navigate ChatGPT,
inspect the ChatGPT DOM, or manipulate credentials/cookies.

In Chrome, manually:

1. Open `chrome://extensions`.
2. Enable Developer mode and load the repository's `extension/` directory as an
   unpacked extension.
3. Open `https://chatgpt.com/`.
4. Log in and complete any security or human verification normally.
5. Leave a ChatGPT tab open with its prompt box visible.
6. Return to the terminal and press Enter.

Only after confirmation does Playwright connect over CDP and verify the user-opened
ChatGPT page. The profile retains Chrome settings, the extension, cookies, and login
state across runs. OutoGPT does not extract or copy authentication data.

The lower-level CLI can perform the same setup while saving a default project URL:

```powershell
gpt setup "https://chatgpt.com/g/<project-id>"
```

## Use

```powershell
outogpt chat create --project-url "https://chatgpt.com/g/..." --prompt-file prompt.md --json
outogpt chat send --chat-id "<conversation-id>" --prompt "Continue" --json
outogpt chat status --chat-id "<conversation-id>" --json
```

A new conversation gets a new tab in the existing CDP-connected Chrome context.
For an existing conversation, the adapter reuses its URL-associated open tab when
possible and creates a replacement tab only when needed. It does not start a new
Chrome process per conversation.

## Startup, shutdown, and recovery

- If the dedicated Chrome instance is already healthy, OutoGPT attaches to it and
  does not treat the process as owned.
- If OutoGPT starts Chrome for a normal one-shot operation, controlled shutdown
  disconnects Playwright and terminates only that owned process.
- Setup deliberately leaves the launched Chrome window open. Later operations attach
  without killing it.
- Closing Chrome never deletes the persistent profile.
- If port 9222 is occupied by another process, close that process or free the port.
- If Chrome reports the dedicated profile is in use without a healthy CDP endpoint,
  close that profile's Chrome window and retry. OutoGPT does not force-open or corrupt
  a locked profile.
- A stale OutoGPT launch record is discarded once its recorded process is no longer
  alive.

No automatic password entry, cookie/token extraction, CAPTCHA bypass, fingerprint
spoofing, stealth patch, or automatic extension installation is implemented.

## Tests

Unit tests mock the Chrome process and CDP boundary and do not require a ChatGPT
login. The live integration test is opt-in and is not part of normal test runs.

## Project Markdown updates

Archive every network-verified conversation in a ChatGPT Project with:

```powershell
outogpt project update --project-url "https://chatgpt.com/g/g-p-.../project"
outogpt project update --project-url "https://chatgpt.com/g/g-p-.../project" --json
outogpt project update --archive-root "D:\ChatGPT archive"
```

When `--project-url` is omitted, the command uses the Project URL saved by
`outogpt setup --project-url ...`. Archive-root precedence is an explicit
`project update --archive-root`, the root saved by `setup --archive-root`,
`OUTOGPT_ARCHIVE_ROOT`, then `~/.outogpt/ChatGPT/`. The folder selected in the
Chrome extension is a browser File System Access handle and is separate because its
absolute OS path is not exposed to the controller. Each project directory contains `project.json`, `index.md`,
and one `chats/<chat-id>.md` file per archived conversation. Project and chat IDs,
not mutable titles, are used to recover existing archives and filenames safely.

CDP network monitoring is registered before navigating to either the project or a
conversation. Project discovery requires a connected request/response cursor chain
ending in an explicit `cursor: null`. Conversation loading requires a connected
`page_info.start_cursor` / request `before` chain ending in
`has_previous_page: false`, with no related requests or response bodies pending.
Scroll position and a temporarily stable DOM are never completion evidence.

Messages observed while scrolling are retained by their network message IDs even
when DOM virtualization removes them. The collected content is checked against all
pages. System/tool/explicitly hidden messages are retained separately as exact
source evidence in the same MD. Verified visible messages, including an unanswered
final user message, are preserved with the existing Markdown converter. Unknown
schemas, unverified content, or ambiguous visible branches pause the update.

The updater retries the current discovery/read/save stage up to three times. It
waits for generation to stop, then reloads that same chat to verify the finished
response. It never skips a failed, loading, or generating chat to visit the next
one. `update-progress.json` records the stage, discovered IDs and pending chat.
Run the same update command to resume; the pending chat is visited first after
project discovery is verified. Previously completed chats are revalidated too.

Existing Markdown bytes are preserved. A changed conversation appends a complete
revision with SHA-256 and length framing, including same-count edits and count
regressions. This trades extra disk space for preservation of earlier versions.
Unchanged revisions are not duplicated. The verified temporary file is atomically
replaced, read back, and compared before `project.json` records chat completion.
Interrupted state writes can adopt an already-saved revision only after its full
content matches. Corrupt snapshots pause the update; legacy MD and unmarked tails
are preserved. `qa_count` describes the latest verified revision, not the sum of
all historical Q/A headings in the file.

Project-update results include `paused` and `pending_chat_id`. Legacy `skipped_*`
fields remain for result compatibility and stay zero. Setup, Chrome profile,
archive-root resolution and the browser lifecycle remain unchanged.

Validation details and the distinction between synthetic tests, Chrome/CDP fixture
tests, and authenticated live-account verification are in
[`outputs/preservation-report.md`](outputs/preservation-report.md).
