# outoGPT Controller

The controller owns the two-phase browser lifecycle: an extension-free,
non-automated manual-authentication Chromium process followed by an
extension-enabled automation process using the same persistent profile. During
automation it maps each conversation to its own tab, records chats and
operations in SQLite, and exposes a CLI.

Install both local packages from the repository root:

```powershell
python -m pip install -e ./CLI-gpt -e ./controller
python -m playwright install chromium
```

Examples:

```powershell
outogpt setup --project-url "https://chatgpt.com/g/..."
outogpt chat create --project-url "https://chatgpt.com/g/..." --prompt-file prompt.md --json
outogpt chat create --prompt "Use the project saved by setup" --json
outogpt chat send --chat-id "<conversation-id>" --prompt "Continue" --json
outogpt chat status --chat-id "<conversation-id>" --json
```

Setup is interactive. OutoGPT initially performs no navigation or page
inspection and does not load the extension. Manually open ChatGPT and finish all
authentication, then return to the terminal and press Enter. Login waiting has
no timeout. Enter triggers verification of the existing tab; failure returns
to manual input without polling or automation.

After successful verification, bootstrap Chromium closes normally and Chromium
restarts with the same `~/.outogpt/chromium-profile` plus the extension. The
controller then verifies the service worker, restored session, and project.

The default passive archive adapter deliberately reports `unconfirmed`. The extension
runs in the same Chromium instance, but this version has no Python-to-extension IPC and
therefore cannot prove that a filesystem write completed.
