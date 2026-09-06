# CLI-GPT

CLI-GPT contains the low-level ChatGPT page actions and OutoGPT's Playwright
browser lifecycle. It does not call an unofficial ChatGPT API.

## Install

```powershell
python -m pip install -e .
python -m playwright install chromium
```

The browser manager always launches Playwright's Chromium channel in headed
mode. It never searches for or falls back to Google Chrome or Edge.

Browser authentication is stored in the dedicated persistent profile
`~/.outogpt/chromium-profile/`. A process-aware lock prevents two Chromium
processes from sharing that profile.

## Standalone compatibility CLI

```powershell
gpt setup "https://chatgpt.com/g/..."
gpt new "Analyze this"
gpt send "https://chatgpt.com/c/..." "Continue"
```

The `gpt` command remains for compatibility. The complete extension-aware
interactive setup and multi-conversation controller are exposed by the
`outogpt` command documented in the repository root.

## Python API

```python
from cli_gpt.chatgpt import create_chat, continue_chat

chat_url = create_chat(PROJECT_URL, "Analyze this")
continue_chat(chat_url, "Continue")
```

Callers that manage multiple chats should use
`cli_gpt.browser.BrowserManager` (or the OutoGPT controller). The manager
provides an extension-free `start_bootstrap()` phase and an extension-enabled
`start_automation()` phase using the same profile. It owns one automation
`BrowserContext`; new conversations receive a new `Page`, and follow-ups
reuse the page registered for their conversation.

## Tests

```powershell
$env:PYTHONPATH = (Resolve-Path .).Path
python -m unittest discover -s tests -v
```

The live test is skipped unless `CLI_GPT_RUN_INTEGRATION=1` and a real project
URL are supplied.
