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
