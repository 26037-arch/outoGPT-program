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


# Project-reading selectors stay in this single module so UI repairs do not leak
# into discovery, storage, or controller code. Links are additionally validated
# against the requested project id before they are accepted.
PROJECT_SPECIFIC_CONVERSATION_REGIONS = (
    '[data-testid="project-conversations"]',
    '[data-testid*="project" i][data-testid*="conversation" i]',
)
PROJECT_CONVERSATION_REGIONS = (
    *PROJECT_SPECIFIC_CONVERSATION_REGIONS,
    'main [role="list"]',
    "main",
)
PROJECT_CHAT_LINKS = ('a[href*="/c/"]',)
PROJECT_SPECIFIC_NAMES = (
    '[data-testid="project-name"]',
    '[aria-label*="Project" i] h1',
)
PROJECT_NAMES = (
    *PROJECT_SPECIFIC_NAMES,
    "main h1",
)
PROJECT_EMPTY_STATES = (
    '[data-testid*="empty" i]',
    '[data-testid*="no-conversation" i]',
)
PROJECT_EMPTY_NAME = re.compile(
    r"no (?:chats|conversations)|start (?:a |your )?(?:chat|conversation)"
    r"|대화가 없습니다|채팅이 없습니다|새 채팅",
    re.IGNORECASE,
)
CONVERSATION_TITLES = (
    '[data-testid="conversation-title"]',
    'nav [aria-current="page"]',
    "main h1",
)
CONVERSATION_EMPTY_STATES = (
    '[data-testid="conversation-empty-state"]',
    '[data-testid*="empty-conversation" i]',
)
MESSAGE_ROOTS = (
    '[data-message-author-role="user"]',
    '[data-message-author-role="assistant"]',
    'article[data-testid^="conversation-turn"] [data-message-author-role]',
)
MESSAGE_UI_EXCLUSIONS = (
    "button",
    "svg",
    '[data-testid*="copy" i]',
    '[data-testid*="feedback" i]',
    '[data-testid*="reaction" i]',
    '[aria-hidden="true"]',
)
PROJECT_ACCESS_ERROR_NAME = re.compile(
    r"not found|no access|do not have access|permission"
    r"|찾을 수 없|접근.*없|권한",
    re.IGNORECASE,
)


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


def project_access_error_visible(page: Any) -> bool:
    return (
        _first_usable(
            [
                page.get_by_role("heading", name=PROJECT_ACCESS_ERROR_NAME),
                page.get_by_role("alert", name=PROJECT_ACCESS_ERROR_NAME),
                page.get_by_text(PROJECT_ACCESS_ERROR_NAME),
            ]
        )
        is not None
    )


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

