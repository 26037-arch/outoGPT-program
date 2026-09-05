"""ChatGPT-specific DOM selectors, kept out of browser orchestration code."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .errors import PromptBoxNotFound


PROMPT_NAME = re.compile(r"message|prompt|chatgpt|메시지|프롬프트", re.IGNORECASE)
SEND_NAME = re.compile(r"send(?: prompt| message)?|보내기|전송", re.IGNORECASE)
STOP_NAME = re.compile(r"stop (?:generating|streaming)|생성 중지|응답 중지", re.IGNORECASE)
NEW_CHAT_NAME = re.compile(r"new chat(?: in .+)?|새 채팅", re.IGNORECASE)
LOGIN_NAME = re.compile(r"log ?in|sign ?in|로그인", re.IGNORECASE)


def _each_match(locator: Any) -> Iterable[Any]:
    try:
        count = locator.count()
    except Exception:
        return
    for index in range(count):
        try:
            yield locator.nth(index)
        except Exception:
            continue


def _usable(locator: Any, *, editable: bool = False) -> bool:
    try:
        if not locator.is_visible() or not locator.is_enabled():
            return False
        return not editable or locator.is_editable()
    except Exception:
        return False


def _first_usable(candidates: Iterable[Any], *, editable: bool = False):
    for candidate in candidates:
        for locator in _each_match(candidate):
            if _usable(locator, editable=editable):
                return locator
    return None


def prompt_box_candidates(page: Any) -> list[Any]:
    return [
        page.get_by_role("textbox", name=PROMPT_NAME),
        page.locator('[aria-label="Prompt"], [aria-label="Message ChatGPT"], [aria-label="메시지"]'),
        page.locator("#prompt-textarea"),
        page.locator('[data-testid="prompt-textarea"]'),
        page.locator('div[contenteditable="true"][role="textbox"]'),
        page.locator('div[contenteditable="true"]'),
        page.locator("textarea"),
    ]


def find_prompt_box(page: Any):
    locator = _first_usable(prompt_box_candidates(page), editable=True)
    if locator is None:
        raise PromptBoxNotFound(
            "Could not find an enabled ChatGPT prompt box. The page may require login or its UI may have changed."
        )
    return locator


def find_send_button(page: Any):
    return _first_usable(
        [
            page.get_by_role("button", name=SEND_NAME),
            page.locator('[data-testid="send-button"]'),
            page.locator('button[aria-label*="Send" i], button[aria-label*="보내기"]'),
        ]
    )


def find_stop_button(page: Any):
    return _first_usable(
        [
            page.get_by_role("button", name=STOP_NAME),
            page.locator('[data-testid="stop-button"]'),
            page.locator('button[aria-label*="Stop" i], button[aria-label*="중지"]'),
        ]
    )


def find_new_chat_control(page: Any):
    return _first_usable(
        [
            page.get_by_role("button", name=NEW_CHAT_NAME),
            page.get_by_role("link", name=NEW_CHAT_NAME),
            page.locator('[data-testid="create-new-chat-button"]'),
        ]
    )


def login_or_challenge_visible(page: Any) -> bool:
    candidates = [
        page.get_by_role("button", name=LOGIN_NAME),
        page.get_by_role("link", name=LOGIN_NAME),
        page.locator('iframe[src*="captcha" i], iframe[src*="challenge" i]'),
        page.locator('[id*="captcha" i], [class*="captcha" i]'),
    ]
    return _first_usable(candidates) is not None


def assistant_response_count(page: Any) -> int:
    counts: list[int] = []
    for selector in (
        '[data-message-author-role="assistant"]',
        'article[data-testid^="conversation-turn"] [data-message-author-role="assistant"]',
    ):
        try:
            counts.append(page.locator(selector).count())
        except Exception:
            continue
    return max(counts, default=0)


def latest_assistant_fingerprint(page: Any) -> str:
    locator = page.locator('[data-message-author-role="assistant"]')
    try:
        count = locator.count()
        if not count:
            return ""
        text = locator.nth(count - 1).inner_text(timeout=500)
        return f"{count}:{len(text)}:{text[-80:]}"
    except Exception:
        return ""

