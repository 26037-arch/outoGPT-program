# OutoGPT

OutoGPT drives ChatGPT in Playwright-managed bundled Chromium and archives
project conversations through the repository's Manifest V3 extension. It never
uses an installed Google Chrome/Edge executable or a personal browser profile.

## Install

```powershell
python -m pip install -e ./CLI-gpt -e ./controller
python -m playwright install chromium
```

If the browser binary is missing, OutoGPT reports
`PLAYWRIGHT_CHROMIUM_NOT_INSTALLED` and shows the install command above.

## First setup: manual authentication boundary

```powershell
outogpt setup --project-url "https://chatgpt.com/g/..."
```

Setup intentionally uses two separate Chromium processes with the same
`~/.outogpt/chromium-profile/` directory.

### Phase 1 — manual bootstrap

1. OutoGPT starts headed bundled Chromium without the extension.
2. OutoGPT does not navigate, inspect the page, poll login, click, type, reload,
   or evaluate JavaScript.
3. In Chromium, manually open ChatGPT and complete login, CAPTCHA, Cloudflare,
   MFA, or any other verification yourself.
4. Return to the terminal and press Enter. There is no timeout.
5. Only after Enter does OutoGPT find the already-open ChatGPT tab and verify
   its authenticated UI.
6. Failed verification returns immediately to blocking terminal input. OutoGPT
   never attempts login or verification automatically.

OutoGPT does not contain CAPTCHA solving, stealth, fingerprint spoofing,
webdriver masking, or authentication-bypass behavior.

### Phase 2 — automation

After login is verified, OutoGPT closes the bootstrap context normally so the
profile is flushed to disk. It then starts bundled Chromium again with the same
profile and these extension flags:

```text
--disable-extensions-except=<repository extension path>
--load-extension=<repository extension path>
```

OutoGPT then verifies:

1. The expected Manifest V3 service worker exists and its runtime ID matches
   its `chrome-extension://` URL.
2. The authenticated ChatGPT session survived the restart.
3. The configured ChatGPT project is accessible.

The automation context remains alive until the controller/program shuts down.
A later process starts directly in Phase 2. If its saved session has expired,
OutoGPT closes automation Chromium and returns to the extension-free manual
bootstrap phase.

The profile lock reports `PROFILE_IN_USE` instead of falling back to a
temporary profile when another OutoGPT Chromium process owns the same profile.

## Commands

```powershell
outogpt chat create --prompt "Analyze the repository" --json
outogpt chat create --project-url "https://chatgpt.com/g/..." --prompt-file prompt.md --json
outogpt chat send --chat-id "<conversation-id>" --prompt "Continue" --json
outogpt chat status --chat-id "<conversation-id>" --json
```

The project verified by `setup` is used when `chat create` omits
`--project-url`. Use `outogpt --help` for the implemented command tree.

## Conversation tabs

One BrowserManager owns the Phase 2 persistent context:

```text
Playwright Chromium
└─ Persistent BrowserContext
   ├─ Conversation A tab
   ├─ Conversation B tab
   ├─ Conversation C tab
   └─ OutoGPT extension service worker
```

A new conversation receives exactly one new tab. Follow-up prompts reuse that
conversation's registered tab. A different conversation receives another tab.
If a registered tab was closed manually, OutoGPT creates one replacement tab
in the same context, reopens the saved conversation URL, and updates the
registry.

The existing extension message protocol and SQLite chat/operation registry are
preserved.

## Tests

```powershell
$env:PYTHONPATH = "$(Resolve-Path ./CLI-gpt)"
python -m unittest discover -s ./CLI-gpt/tests -v

$env:PYTHONPATH = "$(Resolve-Path ./CLI-gpt)$([IO.Path]::PathSeparator)$(Resolve-Path ./controller)"
python -m unittest discover -s ./controller/tests -v

Push-Location ./extension
node --test tests/*.test.js
Pop-Location
```

The default suite does not fake a successful real-account login. Live ChatGPT
authentication and project access require manual verification.
