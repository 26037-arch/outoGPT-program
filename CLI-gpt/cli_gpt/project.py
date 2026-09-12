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
({ rootSelectors, turnSelectors, roleSelectors, titleSelectors, emptySelectors, exclusions }) => {
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
  let invalidTurns = 0;
  const messages = turns.map((turn) => {
    const roleNode = primaryRoleNode(turn);
    if (!roleNode) { invalidTurns += 1; return null; }
    const clone = roleNode.cloneNode(true);
    pruneInactiveChildren(roleNode, clone);
    for (const selector of exclusions) {
      try { clone.querySelectorAll(selector).forEach((node) => node.remove()); } catch (_) {}
    }
    const content = clone.querySelector(".markdown") || clone;
    return { role: roleNode.getAttribute("data-message-author-role"), markdown: clean(render(content)) };
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


def _conversation_sample(page: Any) -> Mapping[str, Any]:
    return page.evaluate(
        _CONVERSATION_SCRIPT,
        {
            "rootSelectors": list(CONVERSATION_ROOTS),
            "turnSelectors": list(CONVERSATION_TURNS),
            "roleSelectors": list(MESSAGE_ROLE_NODES),
            "titleSelectors": list(CONVERSATION_TITLES),
            "emptySelectors": list(CONVERSATION_EMPTY_STATES),
            "exclusions": list(MESSAGE_UI_EXCLUSIONS),
        },
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

    previous: tuple[tuple[str, str], ...] | None = None
    stable = 0
    latest: Mapping[str, Any] | None = None
    for _ in range(max_rounds):
        if login_or_challenge_visible(page) or "/auth/" in getattr(page, "url", ""):
            raise LoginRequired(
                "ChatGPT authentication is required to read the conversation."
            )
        if generation_in_progress(page):
            return ConversationSnapshot(
                chat.chat_id, chat.chat_url, chat.title, (), True
            )
        latest = _conversation_sample(page)
        if latest.get("recognized"):
            messages = latest.get("messages") or ()
            fingerprint = (
                ("__turn_count__", str(latest.get("turnCount") or len(messages))),
                ("__invalid_turns__", str(latest.get("invalidTurns") or 0)),
            ) + tuple(
                (str(item.get("role") or ""), str(item.get("markdown") or ""))
                for item in messages
            )
            stable = stable + 1 if fingerprint == previous else 1
            previous = fingerprint
            if stable >= stable_rounds:
                if generation_in_progress(page):
                    return ConversationSnapshot(
                        chat.chat_id, chat.chat_url, chat.title, (), True
                    )
                if latest.get("invalidTurns"):
                    raise PageStructureChanged(
                        "A conversation turn did not expose exactly one primary user or assistant message."
                    )
                title = str(latest.get("title") or chat.title).strip() or chat.title
                return ConversationSnapshot(
                    chat.chat_id,
                    validate_chat_url(getattr(page, "url", chat.chat_url)),
                    title,
                    pair_messages(messages),
                    False,
                )
        page.wait_for_timeout(poll_ms)
    raise PageStructureChanged(
        f"Conversation {chat.chat_id} did not expose a stable recognizable message DOM."
    )
