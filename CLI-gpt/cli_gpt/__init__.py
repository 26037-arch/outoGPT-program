"""Minimal ChatGPT web adapter."""

from .chatgpt import (
    continue_chat,
    continue_chat_in_page,
    create_chat,
    create_chat_in_page,
)

__all__ = [
    "create_chat",
    "continue_chat",
    "create_chat_in_page",
    "continue_chat_in_page",
]
__version__ = "0.1.0"
