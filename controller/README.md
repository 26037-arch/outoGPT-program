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

The Project URL may be omitted after it has been saved by setup. The default output
is `~/.outogpt/ChatGPT/<sanitized-project-name>/`, containing `project.json`,
`index.md`, and `chats/<chat-id>.md`. One browser session and one reusable page are
used for the entire sequential update; no Chrome process is started per chat.

Only complete user/assistant QA pairs are appended. Generating chats and trailing
unanswered user messages are skipped without blocking other chats. Repeated updates
are idempotent by completed-pair count. Content edits or regenerated answers with an
unchanged pair count are intentionally outside this updater's detection model.
