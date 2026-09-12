"""ChatGPT Project discovery and read-only conversation extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

from .chatgpt import generation_in_progress
from .config import validate_chat_url, validate_project_url
from .errors import (
    InvalidChatUrl,
    InvalidProjectUrl,
    LoginRequired,
    PageStructureChanged,
    ProjectAccessFailed,
)
from .selectors import (
    CONVERSATION_ROOTS,
    CONVERSATION_TITLES,
    CONVERSATION_EMPTY_STATES,
    CONVERSATION_TURNS,
    MESSAGE_ATTACHMENT_IMAGES,
    MESSAGE_ATTACHMENT_NODES,
    MESSAGE_ROLE_NODES,
    MESSAGE_UI_EXCLUSIONS,
    PROJECT_CHAT_LINKS,
    PROJECT_CONVERSATION_REGIONS,
    PROJECT_EMPTY_NAME,
    PROJECT_EMPTY_STATES,
    PROJECT_NAMES,
    PROJECT_SPECIFIC_CONVERSATION_REGIONS,
    PROJECT_SPECIFIC_NAMES,
    login_or_challenge_visible,
    project_access_error_visible,
)


PAGE_LOAD_TIMEOUT_MS = 60_000
DOM_POLL_MS = 200


@dataclass(frozen=True)
class ProjectChat:
    chat_id: str
    chat_url: str
    title: str


@dataclass(frozen=True)
class ProjectDiscovery:
    project_id: str
    project_url: str
    project_name: str
    chats: tuple[ProjectChat, ...]


@dataclass(frozen=True)
class QAPair:
    user: str
    assistant: str


@dataclass(frozen=True)
class ConversationSnapshot:
    chat_id: str
    chat_url: str
    title: str
    qa_pairs: tuple[QAPair, ...]
    generating: bool = False


def extract_project_id(url: str) -> str:
    try:
        parts = [part for part in urlsplit(url).path.split("/") if part]
    except ValueError as exc:
        raise InvalidProjectUrl("The project URL is malformed.") from exc
    project_id = next(
        (part for part in parts if re.fullmatch(r"g-p-[A-Za-z0-9_-]{1,128}", part)),
        "",
    )
    if not project_id:
        raise InvalidProjectUrl(
            "Expected a ChatGPT Project URL containing a g-p- project identifier."
        )
    return project_id


def _chat_belongs_to_project(chat_url: str, project_id: str) -> bool:
    parts = [part for part in urlsplit(chat_url).path.split("/") if part]
    try:
        conversation_index = parts.index("c")
    except ValueError:
        return False
    prefix = parts[:conversation_index]
    embedded_projects = [part for part in prefix if part.startswith("g-p-")]
    return not embedded_projects or project_id in embedded_projects


def extract_chat_id(url: str) -> str:
    try:
        parts = [part for part in urlsplit(url).path.split("/") if part]
        chat_id = parts[parts.index("c") + 1]
    except (ValueError, IndexError) as exc:
        raise PageStructureChanged(
            "A discovered conversation link did not contain /c/<conversation-id>."
        ) from exc
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", chat_id):
        raise PageStructureChanged(
            "A discovered conversation id is not safe to archive."
        )
    return chat_id


_PROJECT_SAMPLE_SCRIPT = r"""
({ regions, specificRegions, links, names, specificNames, emptySelectors, emptyPattern }) => {
  // OUTOGPT_PROJECT_DISCOVERY
  const visible = (element) => {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden"
      && rect.width > 0 && rect.height > 0;
  };
  const all = (root, selectors) => {
    const output = [];
    for (const selector of selectors) {
      try { output.push(...root.querySelectorAll(selector)); } catch (_) {}
    }
    return [...new Set(output)];
  };
  const candidates = all(document, regions).filter(visible);
  const specificCandidates = all(document, specificRegions).filter(visible);
  const region = candidates.find((node) => all(node, links).length > 0)
    || candidates[0] || null;
  const specificRegion = specificCandidates.find((node) => all(node, links).length > 0)
    || specificCandidates[0] || null;
  const anchors = region ? all(region, links) : [];
  const chats = anchors.map((anchor) => ({
    href: anchor.href || anchor.getAttribute("href") || "",
    title: (anchor.getAttribute("aria-label") || anchor.textContent || "").trim()
  })).filter((chat) => chat.href);
  const nameNode = all(document, names).find(visible) || null;
  const specificNameNode = all(document, specificNames).find(visible) || null;
  let name = (nameNode?.textContent || "").trim();
  if (!name) name = String(document.title || "").replace(/\s*[|\-]\s*ChatGPT\s*$/i, "").trim();
  const explicitEmpty = all(document, emptySelectors).some(visible)
    || (region && new RegExp(emptyPattern, "i").test(region.textContent || ""));
  const recognized = Boolean(specificRegion || specificNameNode || chats.length || explicitEmpty);
  return { recognized, ready: Boolean(recognized && name), name, chats, explicitEmpty };
}
"""


_PROJECT_SCROLL_SCRIPT = r"""
(regions) => {
  // OUTOGPT_PROJECT_SCROLL
  const candidates = [];
  for (const selector of regions) {
    try { candidates.push(...document.querySelectorAll(selector)); } catch (_) {}
  }
  let target = null;
  for (const root of candidates) {
    for (const node of [root, ...root.querySelectorAll("*")]) {
      if (node.scrollHeight > node.clientHeight + 1) {
        target = node;
        break;
      }
    }
    if (target) break;
  }
  if (target) {
    const before = target.scrollTop;
    target.scrollTop = target.scrollHeight;
    target.dispatchEvent(new Event("scroll", { bubbles: true }));
    return target.scrollTop !== before;
  }
  const before = window.scrollY;
  window.scrollTo(0, document.documentElement.scrollHeight);
  return window.scrollY !== before;
}
"""


def _project_sample(page: Any) -> Mapping[str, Any]:
    return page.evaluate(
        _PROJECT_SAMPLE_SCRIPT,
        {
            "regions": list(PROJECT_CONVERSATION_REGIONS),
            "specificRegions": list(PROJECT_SPECIFIC_CONVERSATION_REGIONS),
            "links": list(PROJECT_CHAT_LINKS),
            "names": list(PROJECT_NAMES),
            "specificNames": list(PROJECT_SPECIFIC_NAMES),
            "emptySelectors": list(PROJECT_EMPTY_STATES),
            "emptyPattern": PROJECT_EMPTY_NAME.pattern,
        },
    )


def _scroll_project_region(page: Any) -> bool:
    return bool(
        page.evaluate(_PROJECT_SCROLL_SCRIPT, list(PROJECT_CONVERSATION_REGIONS))
    )


def _project_ui_ready(sample: Mapping[str, Any]) -> bool:
    """Return whether a sample contains strong, named project UI evidence."""
    project_name = str(sample.get("name") or "").strip()
    readiness = sample.get("ready")
    if readiness is None:
        readiness = sample.get("recognized")
    return bool(readiness and project_name)


def _check_project_page_state(page: Any, project_id: str, project_url: str) -> None:
    """Keep authentication, access, and redirect checks active while polling."""
    if login_or_challenge_visible(page) or "/auth/" in getattr(page, "url", ""):
        raise LoginRequired("ChatGPT authentication is required to read the project.")
    if project_access_error_visible(page):
        raise ProjectAccessFailed(
            "The ChatGPT project is inaccessible to this account."
        )
    try:
        current_project_id = extract_project_id(getattr(page, "url", project_url))
    except InvalidProjectUrl as exc:
        raise ProjectAccessFailed(
            "ChatGPT redirected away from the requested project."
        ) from exc
    if current_project_id != project_id:
        raise ProjectAccessFailed(
            "ChatGPT redirected away from the requested project."
        )


def discover_project_chats(
    page: Any,
    project_url: str,
    *,
    stable_rounds: int = 3,
    max_rounds: int = 80,
    poll_ms: int = DOM_POLL_MS,
) -> ProjectDiscovery:
    """Wait for project readiness, then discover links using bounded stabilization."""
    project_url = validate_project_url(project_url)
    project_id = extract_project_id(project_url)
    try:
        page.goto(
            project_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS
        )
    except Exception as exc:
        raise ProjectAccessFailed(f"Could not open the ChatGPT project: {exc}") from exc
    _check_project_page_state(page, project_id, project_url)

    ready_sample: Mapping[str, Any] | None = None
    for round_index in range(max_rounds):
        _check_project_page_state(page, project_id, project_url)
        sample = _project_sample(page)
        if _project_ui_ready(sample):
            ready_sample = sample
            break
        if round_index + 1 < max_rounds:
            page.wait_for_timeout(poll_ms)
    if ready_sample is None:
        raise PageStructureChanged(
            "Could not recognize the ChatGPT project conversation list and name."
        )

    discovered: dict[str, ProjectChat] = {}
    stable = 0
    recognized = False
    explicit_empty = False
    project_name = ""
    stabilized = False
    sample = ready_sample
    for round_index in range(max_rounds):
        _check_project_page_state(page, project_id, project_url)
        sample_ready = _project_ui_ready(sample)
        recognized = recognized or bool(sample.get("recognized"))
        explicit_empty = explicit_empty or bool(sample.get("explicitEmpty"))
        candidate_name = str(sample.get("name") or "").strip()
        if candidate_name:
            project_name = candidate_name

        before = len(discovered)
        for item in sample.get("chats") or ():
            raw_url = str(item.get("href") or "").strip()
            if not raw_url:
                continue
            try:
                chat_url = validate_chat_url(urljoin(project_url, raw_url))
            except InvalidChatUrl:
                continue
            if not _chat_belongs_to_project(chat_url, project_id):
                continue
            chat_id = extract_chat_id(chat_url)
            title = str(item.get("title") or "").strip() or "Untitled conversation"
            discovered[chat_id] = ProjectChat(chat_id, chat_url, title)

        if sample_ready and (discovered or explicit_empty):
            stable = stable + 1 if len(discovered) == before else 0
        else:
            stable = 0
        moved = _scroll_project_region(page)
        if stable >= stable_rounds and not moved:
            stabilized = True
            break
        if round_index + 1 < max_rounds:
            page.wait_for_timeout(poll_ms)
            sample = _project_sample(page)

    if not stabilized:
        raise PageStructureChanged(
            "The ChatGPT project conversation list did not stabilize before the scan limit."
        )
    if not recognized or not project_name:
        raise PageStructureChanged(
            "Could not recognize the ChatGPT project conversation list and name."
        )
    if not discovered and not explicit_empty:
        raise PageStructureChanged(
            "The project DOM exposed no conversation links and no explicit empty state."
        )
    return ProjectDiscovery(
        project_id,
        project_url,
        project_name,
        tuple(discovered.values()),
    )


def pair_messages(messages: Iterable[Mapping[str, Any]]) -> tuple[QAPair, ...]:
    """Pair an ordered user/assistant stream without guessing across anomalies."""
    pairs: list[QAPair] = []
    pending_user: str | None = None
    for message in messages:
        role = str(message.get("role") or "")
        markdown = str(message.get("markdown") or "").strip()
        if role not in {"user", "assistant"} or not markdown:
            raise PageStructureChanged(
                "A conversation message had an invalid role or empty body."
            )
        if role == "user":
            if pending_user is not None:
                raise PageStructureChanged(
                    "Two user messages appeared without a safely pairable assistant response."
                )
            pending_user = markdown
            continue
        if pending_user is None:
            raise PageStructureChanged(
                "An assistant message appeared without a preceding user message."
            )
        pairs.append(QAPair(pending_user, markdown))
        pending_user = None
    # One trailing user message is intentionally ignored: it is not a complete QA pair.
    return tuple(pairs)


_CONVERSATION_SCRIPT = r"""
({ rootSelectors, turnSelectors, roleSelectors, attachmentSelectors, imageSelectors,
   titleSelectors, emptySelectors, exclusions }) => {
  // OUTOGPT_CONVERSATION_EXTRACTION
  const text = (node) => String(node?.textContent || "");
  const clean = (value) => String(value || "")
    .replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  const query = (root, selectors) => {
    if (!root || !selectors.length) return [];
    try { return [...root.querySelectorAll(selectors.join(","))]; } catch (_) { return []; }
  };
  const matches = (node, selectors) => {
    if (!node || !selectors.length) return false;
    try { return node.matches(selectors.join(",")); } catch (_) { return false; }
  };
  const inactive = (element, boundary = null) => {
    for (let node = element; node && node.nodeType === Node.ELEMENT_NODE; node = node.parentElement) {
      if (node.hidden || node.hasAttribute("inert")
          || String(node.getAttribute("aria-hidden") || "").toLowerCase() === "true") return true;
      try {
        const style = getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden"
            || style.visibility === "collapse") return true;
      } catch (_) {}
      if (node === boundary) break;
    }
    return false;
  };
  const topLevelTurns = (root) => {
    const candidates = query(root, turnSelectors).filter((turn) => !inactive(turn, root));
    return candidates.filter((turn) => !candidates.some(
      (other) => other !== turn && other.contains(turn)
    ));
  };
  const rootCandidates = query(document, rootSelectors).filter(
    (root) => !matches(root, turnSelectors) && !inactive(root)
  );
  let selected = null;
  for (const root of rootCandidates) {
    const turns = topLevelTurns(root);
    if (!turns.length) continue;
    if (!selected || turns.length > selected.turns.length
        || (turns.length === selected.turns.length && selected.root.contains(root))) {
      selected = { root, turns };
    }
  }
  let conversationRoot = selected?.root || null;
  let turns = selected?.turns || [];
  if (conversationRoot && turns.length) {
    let commonParent = turns[0].parentElement;
    while (commonParent && !turns.every((turn) => commonParent.contains(turn))) {
      commonParent = commonParent.parentElement;
    }
    if (commonParent && conversationRoot.contains(commonParent)) {
      conversationRoot = commonParent;
      turns = topLevelTurns(conversationRoot);
    }
  }
  const ticks = (value, minimum = 1) => {
    const runs = String(value).match(/`+/g) || [];
    return "`".repeat(Math.max(minimum, ...runs.map((run) => run.length + 1)));
  };
  const render = (node, depth = 0) => {
    if (!node) return "";
    if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || "";
    if (node.nodeType !== Node.ELEMENT_NODE) return [...(node.childNodes || [])]
      .map((child) => render(child, depth)).join("");
    const tag = node.tagName.toLowerCase();
    const annotation = node.querySelector?.('annotation[encoding="application/x-tex"]');
    if (annotation && node.matches?.('.katex, .katex-display, [data-math], math')) {
      const source = clean(annotation.textContent || node.getAttribute("data-math"));
      return node.matches('.katex-display') ? `\n\n$$\n${source}\n$$\n\n` : `$${source}$`;
    }
    const inner = () => [...node.childNodes].map((child) => render(child, depth)).join("");
    if (/^h[1-6]$/.test(tag)) return `\n\n${"#".repeat(Number(tag[1]))} ${clean(inner())}\n\n`;
    if (tag === "p") return `\n\n${clean(inner())}\n\n`;
    if (tag === "strong" || tag === "b") return `**${inner()}**`;
    if (tag === "em" || tag === "i") return `*${inner()}*`;
    if (tag === "br") return "\n";
    if (tag === "blockquote") return `\n\n${clean(inner()).split("\n").map((line) => `> ${line}`).join("\n")}\n\n`;
    if (tag === "a") {
      const href = node.getAttribute("href") || "";
      return href ? `[${clean(inner()) || href}](${href})` : inner();
    }
    if (tag === "pre") {
      const code = node.querySelector("code") || node;
      const value = text(code).replace(/\n$/, "");
      const language = [...code.classList].find((name) => name.startsWith("language-"))?.slice(9)
        || code.closest?.("[data-language]")?.getAttribute("data-language") || "";
      const fence = ticks(value, 3);
      return `\n\n${fence}${language}\n${value}\n${fence}\n\n`;
    }
    if (tag === "code" && node.parentElement?.tagName?.toLowerCase() !== "pre") {
      const value = text(node); const fence = ticks(value);
      const pad = value.startsWith(" ") || value.endsWith(" ") ? " " : "";
      return `${fence}${pad}${value}${pad}${fence}`;
    }
    if (tag === "ul" || tag === "ol") {
      const ordered = tag === "ol";
      const items = [...node.children].filter((child) => child.tagName.toLowerCase() === "li");
      return "\n" + items.map((item, index) => {
        const direct = [...item.childNodes].filter((child) => !["UL", "OL"].includes(child.tagName))
          .map((child) => render(child, depth)).join("").trim();
        const prefix = ordered ? `${index + 1}. ` : "- ";
        const nested = [...item.children].filter((child) => ["UL", "OL"].includes(child.tagName))
          .map((child) => render(child, depth + 1)).join("");
        return `${"  ".repeat(depth)}${prefix}${direct}\n${nested}`;
      }).join("") + "\n";
    }
    if (tag === "table") {
      const rows = [...node.querySelectorAll("tr")].map((row) =>
        [...row.querySelectorAll(":scope > th, :scope > td")]
          .map((cell) => clean(render(cell)).replace(/\|/g, "\\|")));
      if (!rows.length) return "";
      const width = Math.max(...rows.map((row) => row.length));
      const normalized = rows.map((row) => [...row, ...Array(width - row.length).fill("")]);
      return `\n\n| ${normalized[0].join(" | ")} |\n| ${Array(width).fill("---").join(" | ")} |\n`
        + normalized.slice(1).map((row) => `| ${row.join(" | ")} |`).join("\n") + "\n\n";
    }
    if (tag === "img") return `![${node.getAttribute("alt") || ""}](${node.getAttribute("src") || ""})`;
    return inner();
  };
  const depthFromTurn = (node, turn) => {
    let depth = 0;
    for (let current = node; current && current !== turn; current = current.parentElement) depth += 1;
    return depth;
  };
  const primaryRoleNode = (turn) => {
    const roleNodes = [
      ...(matches(turn, roleSelectors) ? [turn] : []),
      ...query(turn, roleSelectors)
    ].filter(
      (node) => !inactive(node, turn)
    );
    const roles = new Set(roleNodes.map(
      (node) => node.getAttribute("data-message-author-role")
    ));
    if (roles.size !== 1) return null;
    const outermost = roleNodes.filter((node) => !roleNodes.some(
      (other) => other !== node && other.contains(node)
    ));
    if (!outermost.length) return null;
    const minimumDepth = Math.min(...outermost.map((node) => depthFromTurn(node, turn)));
    const primary = outermost.filter((node) => depthFromTurn(node, turn) === minimumDepth);
    if (primary.length !== 1) return null;
    return primary[0];
  };
  const pruneInactiveChildren = (source, clone) => {
    const sourceChildren = [...source.children];
    const cloneChildren = [...clone.children];
    sourceChildren.forEach((sourceChild, index) => {
      const cloneChild = cloneChildren[index];
      if (!cloneChild) return;
      if (inactive(sourceChild, source)) cloneChild.remove();
      else pruneInactiveChildren(sourceChild, cloneChild);
    });
  };
  const outermostActive = (root, selectors) => {
    const nodes = query(root, selectors).filter((node) => !inactive(node, root));
    return nodes.filter((node) => !nodes.some(
      (other) => other !== node && other.contains(node)
    ));
  };
  const attachmentName = (node) => {
    const generic = /^(?:attachment|uploaded file|file|image attachment|첨부(?: 파일)?|이미지)$/i;
    for (const attribute of ["data-filename", "data-file-name", "download", "title", "aria-label"]) {
      const value = clean(node.getAttribute?.(attribute));
      if (value && !generic.test(value)) return value.slice(0, 260);
    }
    const value = clean(node.textContent);
    return value && value.length <= 260 && !generic.test(value) ? value : "";
  };
  const attachmentEvidence = (roleNode) => {
    if (roleNode.getAttribute("data-message-author-role") !== "user") return [];
    const fileNodes = outermostActive(roleNode, attachmentSelectors);
    const evidence = fileNodes.map((node) => {
      const name = attachmentName(node);
      const descriptor = `${name} ${node.getAttribute?.("data-type") || ""}`.toLowerCase();
      const image = /\.(?:avif|gif|jpe?g|png|webp)(?:\s|$)/i.test(name)
        || descriptor.includes("image") || query(node, imageSelectors).length > 0;
      return { kind: image ? "image" : "file", name };
    });
    for (const image of outermostActive(roleNode, imageSelectors)) {
      if (fileNodes.some((node) => node.contains(image))) continue;
      evidence.push({ kind: "image", name: "" });
    }
    return evidence;
  };
  const stableTurnId = (turn, roleNode) => {
    for (const [attribute, node] of [
      ["data-turn-id", turn],
      ["data-message-id", turn],
      ["data-message-id", roleNode],
      ["data-turn-id", roleNode],
      ["data-testid", turn]
    ]) {
      const value = String(node.getAttribute?.(attribute) || "").trim();
      if (value) return `${attribute}:${value}`;
    }
    return "";
  };
  let invalidTurns = 0;
  const messages = turns.map((turn) => {
    const roleNode = primaryRoleNode(turn);
    if (!roleNode) { invalidTurns += 1; return null; }
    const attachments = attachmentEvidence(roleNode);
    const clone = roleNode.cloneNode(true);
    pruneInactiveChildren(roleNode, clone);
    for (const selector of attachmentSelectors) {
      try { clone.querySelectorAll(selector).forEach((node) => node.remove()); } catch (_) {}
    }
    for (const selector of exclusions) {
      try { clone.querySelectorAll(selector).forEach((node) => node.remove()); } catch (_) {}
    }
    const content = clone.querySelector(".markdown") || clone;
    return {
      turnId: stableTurnId(turn, roleNode),
      role: roleNode.getAttribute("data-message-author-role"),
      markdown: clean(render(content)),
      attachments
    };
  }).filter(Boolean);
  let title = "";
  for (const selector of titleSelectors) {
    const node = document.querySelector(selector);
    if (node && clean(node.textContent)) { title = clean(node.textContent); break; }
  }
  if (!title) title = String(document.title || "").replace(/\s*[|\-]\s*ChatGPT\s*$/i, "").trim();
  let explicitEmpty = false;
  for (const selector of emptySelectors) {
    try {
      if ([...document.querySelectorAll(selector)].some((node) => {
        const style = getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden"
          && rect.width > 0 && rect.height > 0;
      })) explicitEmpty = true;
    } catch (_) {}
  }
  return {
    recognized: turns.length > 0 || explicitEmpty,
    explicitEmpty,
    title,
    turnCount: turns.length,
    invalidTurns,
    messages
  };
}
"""


_CONVERSATION_SCROLL_TOP_SCRIPT = r"""
({ rootSelectors, turnSelectors }) => {
  // OUTOGPT_CONVERSATION_SCROLL_TOP
  const query = (root, selectors) => {
    if (!root || !selectors.length) return [];
    try { return [...root.querySelectorAll(selectors.join(","))]; } catch (_) { return []; }
  };
  const matches = (node, selectors) => {
    try { return node.matches(selectors.join(",")); } catch (_) { return false; }
  };
  const inactive = (element, boundary = null) => {
    for (let node = element; node && node.nodeType === Node.ELEMENT_NODE; node = node.parentElement) {
      if (node.hidden || node.hasAttribute("inert")
          || String(node.getAttribute("aria-hidden") || "").toLowerCase() === "true") return true;
      const style = getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden"
          || style.visibility === "collapse") return true;
      if (node === boundary) break;
    }
    return false;
  };
  const topLevelTurns = (root) => {
    const candidates = query(root, turnSelectors).filter((turn) => !inactive(turn, root));
    return candidates.filter((turn) => !candidates.some(
      (other) => other !== turn && other.contains(turn)
    ));
  };
  let selected = null;
  for (const root of query(document, rootSelectors)) {
    if (matches(root, turnSelectors) || inactive(root)) continue;
    const turns = topLevelTurns(root);
    if (!turns.length) continue;
    if (!selected || turns.length > selected.turns.length
        || (turns.length === selected.turns.length && selected.root.contains(root))) {
      selected = { root, turns };
    }
  }
  if (!selected) return { found: false, atTop: false, wasAtTop: false };
  let conversationRoot = selected.root;
  let commonParent = selected.turns[0].parentElement;
  while (commonParent && !selected.turns.every((turn) => commonParent.contains(turn))) {
    commonParent = commonParent.parentElement;
  }
  if (commonParent && conversationRoot.contains(commonParent)) conversationRoot = commonParent;

  let container = null;
  for (let node = conversationRoot; node; node = node.parentElement) {
    const style = getComputedStyle(node);
    const overflow = String(style.overflowY || "").toLowerCase();
    if (node.scrollHeight > node.clientHeight + 1
        && ["auto", "scroll", "overlay"].includes(overflow)) {
      container = node;
      break;
    }
  }
  let containerKind = "ancestor";
  if (!container) {
    container = document.scrollingElement || null;
    containerKind = "document-fallback";
  }
  if (!container) return { found: false, atTop: false, wasAtTop: false };
  const before = Number(container.scrollTop || 0);
  try { container.scrollTo({ top: 0, behavior: "instant" }); }
  catch (_) { container.scrollTop = 0; }
  container.scrollTop = 0;
  try { container.dispatchEvent(new Event("scroll", { bubbles: true })); } catch (_) {}
  const after = Number(container.scrollTop || 0);
  return {
    found: true,
    containerKind,
    before,
    after,
    wasAtTop: before <= 1,
    atTop: after <= 1,
    scrollHeight: Number(container.scrollHeight || 0),
    clientHeight: Number(container.clientHeight || 0)
  };
}
"""


def _conversation_sample(page: Any) -> Mapping[str, Any]:
    return page.evaluate(
        _CONVERSATION_SCRIPT,
        {
            "rootSelectors": list(CONVERSATION_ROOTS),
            "turnSelectors": list(CONVERSATION_TURNS),
            "roleSelectors": list(MESSAGE_ROLE_NODES),
            "attachmentSelectors": list(MESSAGE_ATTACHMENT_NODES),
            "imageSelectors": list(MESSAGE_ATTACHMENT_IMAGES),
            "titleSelectors": list(CONVERSATION_TITLES),
            "emptySelectors": list(CONVERSATION_EMPTY_STATES),
            "exclusions": list(MESSAGE_UI_EXCLUSIONS),
        },
    )


def _scroll_conversation_history_to_top(page: Any) -> Mapping[str, Any]:
    return page.evaluate(
        _CONVERSATION_SCROLL_TOP_SCRIPT,
        {
            "rootSelectors": list(CONVERSATION_ROOTS),
            "turnSelectors": list(CONVERSATION_TURNS),
        },
    )


_IMAGE_MARKDOWN = re.compile(r"!\[[^\]]*\]\([^\s)]+(?:\s+[^)]*)?\)")


def _normalize_conversation_messages(
    messages: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, str], ...]:
    """Add placeholders only when the DOM reported concrete attachment evidence."""
    normalized: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "")
        markdown = str(message.get("markdown") or "").strip()
        additions: list[str] = []
        for attachment in message.get("attachments") or ():
            if not isinstance(attachment, Mapping):
                continue
            kind = str(attachment.get("kind") or "").strip().lower()
            raw_name = re.sub(r"\s+", " ", str(attachment.get("name") or "")).strip()
            name = raw_name[:260].replace("[", r"\[").replace("]", r"\]")
            if kind == "image":
                if _IMAGE_MARKDOWN.search(markdown):
                    continue
                placeholder = "[Image attachment]"
            elif kind == "file":
                placeholder = f"[Attachment: {name}]" if name else "[Attachment]"
            else:
                continue
            if placeholder not in markdown and placeholder not in additions:
                additions.append(placeholder)
        if additions:
            markdown = "\n\n".join(part for part in (markdown, *additions) if part)
        normalized.append(
            {
                "turnId": str(message.get("turnId") or "").strip(),
                "role": role,
                "markdown": markdown,
            }
        )
    return tuple(normalized)


def _message_history_identity(message: Mapping[str, Any]) -> tuple[str, ...]:
    turn_id = str(message.get("turnId") or "").strip()
    role = str(message.get("role") or "")
    markdown = str(message.get("markdown") or "")
    return ("id", turn_id, role, markdown) if turn_id else ("content", role, markdown)


def _history_fingerprint(
    sample: Mapping[str, Any],
    messages: tuple[dict[str, str], ...],
    scroll_state: Mapping[str, Any],
) -> tuple[Any, ...]:
    identities = tuple(_message_history_identity(message) for message in messages)
    first = identities[0] if identities else ()
    last = identities[-1] if identities else ()
    return (
        int(sample.get("turnCount") or len(messages)),
        first,
        str(messages[0].get("role") or "") if messages else "",
        last,
        str(messages[-1].get("role") or "") if messages else "",
        int(float(scroll_state.get("after") or 0)),
        int(float(scroll_state.get("scrollHeight") or 0)),
        int(sample.get("invalidTurns") or 0),
        identities,
    )


def _stable_turn_ids(messages: tuple[dict[str, str], ...]) -> tuple[str, ...] | None:
    identifiers = tuple(message["turnId"] for message in messages)
    if not identifiers or any(not identifier for identifier in identifiers):
        return None
    return identifiers if len(set(identifiers)) == len(identifiers) else None


def _merge_identified_history(
    existing: tuple[dict[str, str], ...],
    current: tuple[dict[str, str], ...],
) -> tuple[dict[str, str], ...]:
    existing_ids = _stable_turn_ids(existing)
    current_ids = _stable_turn_ids(current)
    if existing_ids is None or current_ids is None:
        raise PageStructureChanged(
            "Virtualized conversation turns did not expose stable unique identities."
        )
    existing_by_id = dict(zip(existing_ids, existing))
    current_by_id = dict(zip(current_ids, current))
    shared_current = [identifier for identifier in current_ids if identifier in existing_by_id]
    shared_existing = [identifier for identifier in existing_ids if identifier in current_by_id]
    if not shared_current or shared_current != shared_existing:
        raise PageStructureChanged(
            "Conversation history windows could not be merged without guessing turn order."
        )
    for identifier in shared_current:
        old = existing_by_id[identifier]
        new = current_by_id[identifier]
        if old["role"] != new["role"]:
            raise PageStructureChanged(
                "A conversation turn identity changed role while history was loading."
            )

    merged: list[dict[str, str]] = []
    existing_index = 0
    current_index = 0
    for identifier in shared_current:
        next_existing = existing_ids.index(identifier, existing_index)
        next_current = current_ids.index(identifier, current_index)
        existing_gap = existing[existing_index:next_existing]
        current_gap = current[current_index:next_current]
        if existing_gap and current_gap:
            raise PageStructureChanged(
                "Conversation history windows contained an ambiguous gap between turns."
            )
        merged.extend(current_gap or existing_gap)
        merged.append(current_by_id[identifier])
        existing_index = next_existing + 1
        current_index = next_current + 1
    existing_tail = existing[existing_index:]
    current_tail = current[current_index:]
    if existing_tail and current_tail:
        raise PageStructureChanged(
            "Conversation history windows contained an ambiguous trailing gap."
        )
    merged.extend(current_tail or existing_tail)
    return tuple(merged)


def _contains_message_sequence(
    larger: tuple[dict[str, str], ...], smaller: tuple[dict[str, str], ...]
) -> bool:
    if len(smaller) > len(larger):
        return False
    smaller_keys = tuple(_message_history_identity(message) for message in smaller)
    larger_keys = tuple(_message_history_identity(message) for message in larger)
    return any(
        larger_keys[index : index + len(smaller_keys)] == smaller_keys
        for index in range(len(larger_keys) - len(smaller_keys) + 1)
    )


def _merge_history_messages(
    existing: tuple[dict[str, str], ...],
    current: tuple[dict[str, str], ...],
) -> tuple[dict[str, str], ...]:
    if not existing:
        return current
    if not current:
        return existing
    if _stable_turn_ids(existing) is not None and _stable_turn_ids(current) is not None:
        return _merge_identified_history(existing, current)
    if _contains_message_sequence(current, existing):
        return current
    if _contains_message_sequence(existing, current):
        return existing
    raise PageStructureChanged(
        "Virtualized conversation history changed without stable turn identities."
    )


def _hydrate_conversation_history(
    page: Any,
    *,
    stable_rounds: int,
    max_rounds: int,
    poll_ms: int,
) -> Mapping[str, Any] | None:
    """Load older turns at the real scroll top before strict QA pairing."""
    previous: tuple[Any, ...] | None = None
    stable = 0
    accumulated: tuple[dict[str, str], ...] = ()
    for round_index in range(max_rounds):
        if login_or_challenge_visible(page) or "/auth/" in getattr(page, "url", ""):
            raise LoginRequired(
                "ChatGPT authentication is required to read the conversation."
            )
        if generation_in_progress(page):
            return None
        sample = _conversation_sample(page)
        if sample.get("recognized"):
            messages = _normalize_conversation_messages(sample.get("messages") or ())
            if not sample.get("invalidTurns"):
                accumulated = _merge_history_messages(accumulated, messages)
            if sample.get("explicitEmpty") and not messages:
                scroll_state: Mapping[str, Any] = {
                    "found": True,
                    "wasAtTop": True,
                    "atTop": True,
                    "after": 0,
                    "scrollHeight": 0,
                }
            else:
                scroll_state = _scroll_conversation_history_to_top(page)
            fingerprint = _history_fingerprint(sample, messages, scroll_state)
            at_stable_top = bool(
                scroll_state.get("found")
                and scroll_state.get("wasAtTop")
                and scroll_state.get("atTop")
            )
            if at_stable_top:
                stable = stable + 1 if fingerprint == previous else 1
            else:
                stable = 0
            previous = fingerprint
            if stable >= stable_rounds:
                if generation_in_progress(page):
                    return None
                if sample.get("invalidTurns"):
                    raise PageStructureChanged(
                        "A conversation turn did not expose exactly one primary user or assistant message."
                    )
                complete = dict(sample)
                complete["messages"] = accumulated
                complete["turnCount"] = len(accumulated)
                return complete
        if round_index + 1 < max_rounds:
            page.wait_for_timeout(poll_ms)
    raise PageStructureChanged(
        "The complete conversation history did not stabilize before the scan limit."
    )


def read_conversation(
    page: Any,
    chat: ProjectChat,
    *,
    stable_rounds: int = 2,
    max_rounds: int = 50,
    poll_ms: int = DOM_POLL_MS,
) -> ConversationSnapshot:
    """Open one chat, skip active generation, and extract complete QA pairs."""
    try:
        page.goto(
            chat.chat_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS
        )
    except Exception as exc:
        raise PageStructureChanged(
            f"Could not load conversation {chat.chat_id}: {exc}"
        ) from exc
    if login_or_challenge_visible(page) or "/auth/" in getattr(page, "url", ""):
        raise LoginRequired(
            "ChatGPT authentication is required to read the conversation."
        )
    try:
        loaded_url = validate_chat_url(getattr(page, "url", chat.chat_url))
    except InvalidChatUrl as exc:
        raise PageStructureChanged(
            f"Conversation {chat.chat_id} redirected away from a conversation page."
        ) from exc
    if extract_chat_id(loaded_url) != chat.chat_id:
        raise PageStructureChanged(
            f"Conversation {chat.chat_id} redirected to a different conversation."
        )

    complete = _hydrate_conversation_history(
        page,
        stable_rounds=stable_rounds,
        max_rounds=max_rounds,
        poll_ms=poll_ms,
    )
    if complete is None:
        return ConversationSnapshot(chat.chat_id, chat.chat_url, chat.title, (), True)
    title = str(complete.get("title") or chat.title).strip() or chat.title
    return ConversationSnapshot(
        chat.chat_id,
        validate_chat_url(getattr(page, "url", chat.chat_url)),
        title,
        pair_messages(complete.get("messages") or ()),
        False,
    )
