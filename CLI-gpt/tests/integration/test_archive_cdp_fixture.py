"""Real Chrome/CDP with synthetic routed responses; never a live account check.

Run explicitly with OUTOGPT_CDP_FIXTURE=1. Uses the unchanged dedicated profile
and BrowserSession lifecycle. No archive files or ChatGPT account data are changed.
"""

import os
import unittest

from cli_gpt.browser import BrowserSession
from cli_gpt.project import ProjectChat, read_conversation


def message(identifier, parent, role, text):
    return {
        "id": identifier,
        "parent": parent,
        "message": {
            "id": identifier,
            "author": {"role": role},
            "status": "finished_successfully",
            "content": {"content_type": "text", "parts": [text]},
        },
    }


def payload(nodes, previous, cursor):
    return {
        "mapping": {n["id"]: n for n in nodes},
        "page_info": {"has_previous_page": previous, "start_cursor": cursor},
    }


@unittest.skipUnless(
    os.environ.get("OUTOGPT_CDP_FIXTURE") == "1",
    "Opt-in real Chrome synthetic CDP fixture",
)
class ChromeFixtureTests(unittest.TestCase):
    def test_delayed_pagination_virtualization_and_hidden_system_boundary(self):
        recent = payload(
            [
                message("boundary", "a1", "system", "hidden boundary"),
                message("u2", "boundary", "user", "Second question"),
                message("a2", "u2", "assistant", "Second answer"),
            ],
            True,
            "boundary",
        )
        older = payload(
            [
                message("root", None, "system", "hidden root"),
                message("u1", "root", "user", "First question"),
                message("a1", "u1", "assistant", "First answer"),
            ],
            False,
            "root",
        )
        html = """<!doctype html><html><head><title>Fixture</title></head><body><main></main>
        <script>
        const render = (rows) => document.querySelector('main').innerHTML = rows.map(
          ([id,role,text],i) => `<article data-testid="conversation-turn-${i}"><div
            data-message-author-role="${role}" data-message-id="${id}"><div class="markdown">${text}</div>
          </div></article>`).join('');
        let ready=false, requested=false;
        fetch('/backend-api/conversation/fixture').then(r=>r.json()).then(()=>{
          render([['u2','user','Second question'],['a2','assistant','Second answer']]); ready=true;
        });
        document.addEventListener('scroll',()=>{
          if(!ready || requested) return; requested=true;
          setTimeout(()=>fetch('/backend-api/conversation/fixture/messages?before=boundary')
            .then(r=>r.json()).then(()=>setTimeout(()=>render(
              [['u1','user','First question'],['a1','assistant','First answer']]),400)),500);
        },true);
        </script></body></html>"""
        with BrowserSession() as browser:
            page = browser.new_page()
            try:

                def route(request):
                    url = request.request.url
                    if "/backend-api/conversation/fixture/messages" in url:
                        request.fulfill(json=older)
                    elif "/backend-api/conversation/fixture" in url:
                        request.fulfill(json=recent)
                    else:
                        request.fulfill(body=html, content_type="text/html")

                page.route("https://chatgpt.com/**", route)
                snapshot = read_conversation(
                    page,
                    ProjectChat("fixture", "https://chatgpt.com/c/fixture", "Fixture"),
                    max_rounds=150,
                    poll_ms=100,
                )
                self.assertEqual(len(snapshot.qa_pairs), 2)
                self.assertEqual(
                    [m["id"] for m in snapshot.messages], ["u1", "a1", "u2", "a2"]
                )
                self.assertEqual(len(snapshot.non_ui_messages), 2)
                self.assertEqual(snapshot.qa_pairs[0].user, "First question")
                self.assertEqual(snapshot.qa_pairs[-1].assistant, "Second answer")
            finally:
                page.close()
