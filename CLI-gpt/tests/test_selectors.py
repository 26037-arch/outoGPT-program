import unittest

from cli_gpt.errors import PromptBoxNotFound
from cli_gpt.selectors import find_prompt_box


class FakeElement:
    def __init__(self, name, *, visible=True, enabled=True, editable=True):
        self.name = name
        self.visible = visible
        self.enabled = enabled
        self.editable = editable

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def is_editable(self):
        return self.editable


class FakeLocator:
    def __init__(self, elements=()):
        self.elements = list(elements)

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]


class FakePage:
    def __init__(self, selectors=None, roles=None):
        self.selectors = selectors or {}
        self.roles = roles or {}

    def locator(self, selector):
        return FakeLocator(self.selectors.get(selector, ()))

    def get_by_role(self, role, name=None):
        return FakeLocator(self.roles.get(role, ()))


class SelectorTests(unittest.TestCase):
    def test_falls_back_to_prompt_id(self):
        hidden_role = FakeElement("role", visible=False)
        fallback = FakeElement("prompt-id")
        page = FakePage(
            selectors={"#prompt-textarea": [fallback]},
            roles={"textbox": [hidden_role]},
        )
        self.assertIs(find_prompt_box(page), fallback)

    def test_skips_non_editable_match(self):
        not_editable = FakeElement("bad", editable=False)
        textarea = FakeElement("textarea")
        page = FakePage(
            selectors={
                "#prompt-textarea": [not_editable],
                "textarea": [textarea],
            }
        )
        self.assertIs(find_prompt_box(page), textarea)

    def test_raises_when_no_candidate_is_usable(self):
        with self.assertRaises(PromptBoxNotFound):
            find_prompt_box(FakePage())


if __name__ == "__main__":
    unittest.main()

