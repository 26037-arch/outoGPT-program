# CLI-GPT

CLI-GPT keeps ChatGPT-specific DOM automation separate from browser ownership. Its
core APIs continue to accept a caller-owned Playwright `Page`:

```python
create_chat_in_page(page, project_url, prompt)
continue_chat_in_page(page, chat_url, prompt)
```

## Chrome and CDP

Normal operation does not launch Playwright Chromium. CLI-GPT finds installed Google
Chrome, starts it with `subprocess.Popen`, gives it the persistent
`data/browser-profile/` directory, and connects with
`playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")`.

Chrome discovery checks `OUTOGPT_CHROME_PATH`, the normal Program Files and Local App
Data installation locations on Windows, and Chrome executables available on `PATH`.
Failure to find Chrome is reported as a domain error.

Install the Python package only; no Playwright browser download is needed:

```powershell
python -m pip install -e ./CLI-gpt
```

## Manual setup

```powershell
gpt setup "https://chatgpt.com/g/<project-id>"
```

Chrome opens before Playwright attaches. Manually open `chrome://extensions`, load
the repository's unpacked `extension/` directory, open ChatGPT, log in, finish any
security checks, and then press Enter. After confirmation, CLI-GPT attaches and
checks the prompt box. Successful setup writes a local readiness marker and saves the
project URL.

`gpt new` and `gpt send` will not start ChatGPT automation until that readiness marker
exists. They do not implement an automatic login flow.

## Commands

```powershell
gpt new "Analyze this idea"
gpt send "https://chatgpt.com/c/..." "Continue the analysis"
```

The output includes the completed conversation URL. ChatGPT selectors and generation
tracking remain in `cli_gpt/chatgpt.py` and `cli_gpt/selectors.py`; Chrome lifecycle,
ownership, locking, and CDP attachment live in `cli_gpt/browser.py`.

## Lifecycle policy

- Setup leaves an OutoGPT-launched Chrome process open so its manually prepared
  window is immediately usable.
- A later session that attaches to that recorded process disconnects Playwright on
  close and does not terminate Chrome.
- A normal operation that had to launch its own dedicated Chrome may terminate that
  owned process during controlled shutdown.
- The persistent profile is never deleted by shutdown.
- Port collisions, missing Chrome, unavailable CDP, locked profiles, missing contexts,
  and unexpected Chrome closure have explicit domain errors.

The CDP server is requested on `127.0.0.1:9222`; it is not intentionally exposed to
the LAN.

