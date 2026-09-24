# OutoGPT Controller

The controller keeps the existing SQLite registry, chat ID persistence, operation
state transitions, passive archive adapter, and CLI-GPT page-action APIs.

The browser adapter now opens one `BrowserSession` connected to ordinary installed
Google Chrome over local CDP. New chats receive a new tab. Continuing a chat reuses
an open page with the stored conversation URL, or creates a new tab when that page is
closed. The adapter does not load or verify the extension through Playwright; the
extension is installed manually once into the persistent OutoGPT Chrome profile.

## Install and setup

```powershell
python -m pip install -e ./CLI-gpt -e ./controller
outogpt setup
```

During setup, complete Chrome extension installation, ChatGPT login, and any security
verification manually. Playwright attaches only after you press Enter. You may also
save CLI-GPT's default project at the same time:

```powershell
outogpt setup --project-url "https://chatgpt.com/g/..."
```

`playwright install chromium` is not required for normal controller operation.

## Commands

```powershell
outogpt chat create --project-url "https://chatgpt.com/g/..." --prompt-file prompt.md --json
outogpt chat send --chat-id "<conversation-id>" --prompt "Continue" --json
outogpt chat status --chat-id "<conversation-id>" --json
```

The passive archive adapter still reports `unconfirmed`: the installed extension can
archive the page, but this milestone does not add Python-to-extension completion IPC.

## Update a whole Project archive

```powershell
outogpt project update --project-url "https://chatgpt.com/g/g-p-.../project"
outogpt project update --archive-root "D:\ChatGPT archive" --json
```

The Project URL may be omitted after it has been saved by setup. Save a persistent
controller destination with `outogpt setup --archive-root "D:\ChatGPT archive"`.
Resolution order is the update command option, saved controller configuration,
`OUTOGPT_ARCHIVE_ROOT`, then the default. The Chrome extension's folder picker is
separate because its File System Access handle does not reveal an absolute OS path
to Python. The default output is `~/.outogpt/ChatGPT/<sanitized-project-name>/`, containing `project.json`,
`index.md`, and `chats/<chat-id>.md`. One browser session and one reusable page are
used for the entire sequential update; no Chrome process is started per chat.

The updater verifies network pagination and every observed message before saving.
Loading or generation errors retry the same chat and then pause the entire update;
no later chat is visited. Rerun the command to resume from `update-progress.json`.

Each MD preserves existing bytes and appends a content-verified revision when the
conversation changes, including edits with unchanged QA counts. Unanswered user
messages and explicitly non-UI messages are preserved in the same file. Completion
in `project.json` is recorded only after the actual file contents are read back and
verified. Unknown response formats or unverifiable messages remain pending.

See the repository README and `outputs/preservation-report.md` for the completion
contract, supported evidence, test commands, and live-validation limitations.
