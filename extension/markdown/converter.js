import { extractCodeLanguage, extractMath } from "./chatgpt-rules.js";

const TEXT_NODE = 3;
const ELEMENT_NODE = 1;

function children(node, context) {
  return [...(node.childNodes ?? [])].map((child) => nodeToMarkdown(child, context)).join("");
}

function cleanInline(text) {
  return text.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n");
}

function longestBacktickRun(text) {
  return Math.max(0, ...([...text.matchAll(/`+/g)].map((match) => match[0].length)));
}

function renderList(node, ordered, depth) {
  const items = [...(node.children ?? [])].filter((child) => child.tagName?.toLowerCase() === "li");
  return items.map((item, index) => {
    const direct = [...(item.childNodes ?? [])]
      .filter((child) => !["ul", "ol"].includes(child.tagName?.toLowerCase()))
      .map((child) => nodeToMarkdown(child, { listDepth: depth }))
      .join("")
      .trim();
    const prefix = ordered ? `${index + 1}. ` : "- ";
    const indentation = "  ".repeat(depth);
    const continuationIndent = " ".repeat(prefix.length);
    const body = direct.replace(/\n/g, `\n${indentation}${continuationIndent}`);
    const nested = [...(item.children ?? [])]
      .filter((child) => ["ul", "ol"].includes(child.tagName?.toLowerCase()))
      .map((child) => renderList(child, child.tagName.toLowerCase() === "ol", depth + 1))
      .join("");
    return `${indentation}${prefix}${body}\n${nested}`;
  }).join("");
}

function renderTable(node, context) {
  const rows = [...node.querySelectorAll("tr")].map((row) =>
    [...row.querySelectorAll(":scope > th, :scope > td")]
      .map((cell) => cleanInline(children(cell, context)).trim().replace(/\|/g, "\\|"))
  ).filter((row) => row.length);
  if (!rows.length) return "";
  const width = Math.max(...rows.map((row) => row.length));
  const normalized = rows.map((row) => [...row, ...Array(width - row.length).fill("")]);
  const header = normalized[0];
  const separator = Array(width).fill("---");
  return `\n\n| ${header.join(" | ")} |\n| ${separator.join(" | ")} |\n${normalized.slice(1).map((row) => `| ${row.join(" | ")} |`).join("\n")}\n\n`;
}

export function nodeToMarkdown(node, context = { listDepth: 0 }) {
  if (!node) return "";
  if (node.nodeType === TEXT_NODE) return node.nodeValue ?? node.textContent ?? "";
  if (node.nodeType !== ELEMENT_NODE) return children(node, context);

  const math = extractMath(node);
  if (math) return math;
  const tag = node.tagName.toLowerCase();
  const inner = () => children(node, context);

  if (/^h[1-6]$/.test(tag)) return `\n\n${"#".repeat(Number(tag[1]))} ${cleanInline(inner()).trim()}\n\n`;
  if (tag === "p") return `\n\n${cleanInline(inner()).trim()}\n\n`;
  if (tag === "strong" || tag === "b") return `**${inner()}**`;
  if (tag === "em" || tag === "i") return `*${inner()}*`;
  if (tag === "br") return "\n";
  if (tag === "hr") return "\n\n---\n\n";
  if (tag === "ul" || tag === "ol") return `\n${renderList(node, tag === "ol", context.listDepth ?? 0)}\n`;
  if (tag === "li") return inner();
  if (tag === "blockquote") {
    const value = cleanInline(inner()).trim().split("\n").map((line) => `> ${line}`).join("\n");
    return `\n\n${value}\n\n`;
  }
  if (tag === "a") {
    const href = node.getAttribute("href") || "";
    return href ? `[${cleanInline(inner()).trim() || href}](${href})` : inner();
  }
  if (tag === "pre") {
    const code = node.querySelector("code") ?? node;
    const value = (code.textContent ?? "").replace(/\n$/, "");
    const fence = "`".repeat(Math.max(3, longestBacktickRun(value) + 1));
    return `\n\n${fence}${extractCodeLanguage(code)}\n${value}\n${fence}\n\n`;
  }
  if (tag === "code" && node.parentElement?.tagName?.toLowerCase() !== "pre") {
    const value = node.textContent ?? "";
    const fence = "`".repeat(Math.max(1, longestBacktickRun(value) + 1));
    const padding = value.startsWith(" ") || value.endsWith(" ") ? " " : "";
    return `${fence}${padding}${value}${padding}${fence}`;
  }
  if (tag === "table") return renderTable(node, context);
  if (tag === "img") return `![${node.getAttribute("alt") || ""}](${node.getAttribute("src") || ""})`;
  return inner();
}

function yamlString(value) {
  return JSON.stringify(String(value ?? ""));
}

export function renderConversationMarkdown(snapshot, updatedAt) {
  const title = snapshot.title || "Untitled conversation";
  const frontmatter = [
    "---",
    `conversation_id: ${yamlString(snapshot.conversationId)}`,
    `project_id: ${yamlString(snapshot.projectId)}`,
    `project_name: ${yamlString(snapshot.projectName)}`,
    `title: ${yamlString(title)}`,
    `url: ${yamlString(snapshot.url)}`,
    `updated_at: ${yamlString(updatedAt)}`,
    "---",
    ""
  ].join("\n");
  const messages = snapshot.messages.map(({ role, markdown }) => {
    const heading = role === "assistant" ? "Assistant" : "User";
    return `## ${heading}\n\n${String(markdown ?? "").trim()}`;
  }).join("\n\n");
  return `${frontmatter}# ${title}\n\n${messages}\n`;
}

