# outoGPT Controller

The controller owns one CLI-GPT browser session per operation, loads the repository's
unpacked Chrome extension into that session, records chats and operations in SQLite,
and exposes a non-interactive CLI.

Install both local packages from the repository root:

```powershell
python -m pip install -e ./CLI-gpt -e ./controller
playwright install chromium
```

Examples:

```powershell
outogpt chat create --project-url "https://chatgpt.com/g/..." --prompt-file prompt.md --json
outogpt chat send --chat-id "<conversation-id>" --prompt "Continue" --json
outogpt chat status --chat-id "<conversation-id>" --json
```

The default passive archive adapter deliberately reports `unconfirmed`. The extension
runs in the same Chromium instance, but this version has no Python-to-extension IPC and
therefore cannot prove that a filesystem write completed.
