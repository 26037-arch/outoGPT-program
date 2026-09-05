import test from "node:test";
import assert from "node:assert/strict";
import { nodeToMarkdown, renderConversationMarkdown } from "../markdown/converter.js";

function text(value) {
  return { nodeType: 3, nodeValue: value, textContent: value };
}

function element(tag, childNodes = [], attributes = {}) {
  const node = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    childNodes,
    children: childNodes.filter((child) => child.nodeType === 1),
    className: attributes.class ?? "",
    parentElement: null,
    getAttribute(name) { return attributes[name] ?? null; },
    matches(selector) {
      return selector.split(",").some((part) => part.trim() === `.${attributes.class}` || part.trim() === tag);
    },
    closest() { return null; },
    querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; },
    querySelectorAll(selector) {
      if (selector === ":scope > th, :scope > td") return this.children.filter((child) => ["TH", "TD"].includes(child.tagName));
      const wanted = selector.toUpperCase();
      const output = [];
      function visit(current) {
        for (const child of current.children ?? []) {
          if (child.tagName === wanted) output.push(child);
          visit(child);
        }
      }
      visit(this);
      return output;
    }
  };
  for (const child of childNodes) if (child.nodeType === 1) child.parentElement = node;
  Object.defineProperty(node, "textContent", {
    get() { return childNodes.map((child) => child.textContent ?? child.nodeValue ?? "").join(""); }
  });
  return node;
}

test("Markdown preserves headings, emphasis, links and blockquotes", () => {
  assert.match(nodeToMarkdown(element("h2", [text("Heading")])), /## Heading/);
  assert.equal(nodeToMarkdown(element("strong", [text("bold")])), "**bold**");
  assert.equal(nodeToMarkdown(element("em", [text("italic")])), "*italic*");
  assert.equal(nodeToMarkdown(element("a", [text("OpenAI")], { href: "https://openai.com" })), "[OpenAI](https://openai.com)");
  assert.match(nodeToMarkdown(element("blockquote", [text("quoted")])), /> quoted/);
});

test("Markdown preserves nested ordered and unordered lists", () => {
  const nested = element("ul", [
    element("li", [text("outer"), element("ol", [element("li", [text("inner")])])])
  ]);
  const markdown = nodeToMarkdown(nested);
  assert.match(markdown, /- outer/);
  assert.match(markdown, /  1\. inner/);
});

test("Markdown preserves fenced code language and inline code", () => {
  const code = element("code", [text("const x = 1;")], { class: "language-js" });
  const markdown = nodeToMarkdown(element("pre", [code]));
  assert.match(markdown, /```js\nconst x = 1;/);
  assert.equal(nodeToMarkdown(element("code", [text("x")])), "`x`");
});

test("Markdown preserves a table", () => {
  const table = element("table", [
    element("tr", [element("th", [text("A")]), element("th", [text("B")])]),
    element("tr", [element("td", [text("1")]), element("td", [text("2")])])
  ]);
  const markdown = nodeToMarkdown(table);
  assert.match(markdown, /\| A \| B \|/);
  assert.match(markdown, /\| --- \| --- \|/);
});

test("Markdown preserves LaTeX source", () => {
  const annotation = element("annotation", [text("x^2")], { encoding: "application/x-tex" });
  const math = element("span", [annotation], { class: "katex", "data-math": "x^2" });
  math.querySelector = () => annotation;
  assert.equal(nodeToMarkdown(math), "$x^2$");
});

test("a nested equation does not replace the surrounding response", () => {
  const annotation = element("annotation", [text("x^2")], { encoding: "application/x-tex" });
  const math = element("span", [annotation], { class: "katex" });
  math.querySelector = () => annotation;
  const response = element("div", [
    element("p", [text("Before")]),
    math,
    element("p", [text("After")])
  ], { class: "markdown" });
  const markdown = nodeToMarkdown(response);
  assert.match(markdown, /Before/);
  assert.match(markdown, /\$x\^2\$/);
  assert.match(markdown, /After/);
});

test("conversation document contains frontmatter and ordered roles", () => {
  const output = renderConversationMarkdown({
    conversationId: "c1",
    projectId: "p1",
    projectName: "Project",
    title: "Title",
    url: "https://chatgpt.com/c/c1",
    messages: [
      { role: "user", markdown: "Question" },
      { role: "assistant", markdown: "Answer" }
    ]
  }, "2026-09-06T00:00:00+09:00");
  assert.match(output, /^---\nconversation_id: "c1"/);
  assert.ok(output.indexOf("## User") < output.indexOf("## Assistant"));
});
