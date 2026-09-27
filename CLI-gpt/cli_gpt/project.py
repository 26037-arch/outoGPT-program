"""ChatGPT Project discovery and read-only conversation extraction."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from .chatgpt import generation_in_progress
from .config import validate_chat_url, validate_project_url
from .errors import (
    ConversationHistoryIncomplete,
    ConversationLoadingUnknown,
    ConversationStructureError,
    InvalidChatUrl,
    InvalidProjectUrl,
    LoginRequired,
    PageStructureChanged,
    ProjectAccessFailed,
)
from .pagination import PaginationEvidence
from .selectors import (
    CONVERSATION_ROOTS,
    CONVERSATION_TITLES,
    CONVERSATION_EMPTY_STATES,
    CONVERSATION_LOADING_INDICATORS,
    CONVERSATION_TURNS,
    MESSAGE_ATTACHMENT_IMAGES,
    MESSAGE_ATTACHMENT_NODES,
    MESSAGE_AUXILIARY_NODES,
    MESSAGE_ROLE_NODES,
    MESSAGE_UI_EXCLUSIONS,
    PROJECT_CHAT_LINKS,
    PROJECT_CONVERSATION_REGIONS,
    PROJECT_EMPTY_NAME,
    PROJECT_EMPTY_STATES,
    PROJECT_END_INDICATORS,
    PROJECT_LOAD_MORE_CONTROLS,
    PROJECT_LOADING_INDICATORS,
    PROJECT_NAMES,
    PROJECT_SPECIFIC_CONVERSATION_REGIONS,
    PROJECT_SPECIFIC_NAMES,
    login_or_challenge_visible,
    project_access_error_visible,
)


PAGE_LOAD_TIMEOUT_MS = 60_000
DOM_POLL_MS = 200
ProjectProgress = Callable[[str, Mapping[str, Any]], None]


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
    complete: bool = True
    diagnostic: str | None = None


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
    status: str = "complete"
    diagnostic: str | None = None
    messages: tuple[Mapping[str, Any], ...] = ()
    non_ui_messages: tuple[Mapping[str, Any], ...] = ()


def extract_project_id(url: str) -> str:
    try:
        parts = [part for part in urlsplit(url).path.split("/") if part]
    except ValueError as exc:
        raise InvalidProjectUrl("The project URL is malformed.") from exc
    for part in parts:
        match = re.match(r"^(g-p-[0-9a-fA-F]{32})(?=-|$)", part)
        if match:
            return match.group(1)
    raise InvalidProjectUrl("Could not extract the canonical project ID.")


def _chat_belongs_to_project(
    chat_url: str, project_id: str, *, project_scoped: bool = False
) -> bool:
    parts = [part for part in urlsplit(chat_url).path.split("/") if part]
    try:
        conversation_index = parts.index("c")
    except ValueError:
        return False
    prefix = parts[:conversation_index]
    embedded_projects = [part for part in prefix if part.startswith("g-p-")]
    return project_id in embedded_projects or (project_scoped and not embedded_projects)


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


def project_chat_url(project_url: str, chat_id: str) -> str:
    """Build the canonical route used by conversation links on project pages."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", chat_id):
        raise PageStructureChanged("A project response contained an unsafe chat ID.")
    parts = urlsplit(project_url)
    path_parts = [part for part in parts.path.split("/") if part]
    project_index = next(
        (
            index
            for index, part in enumerate(path_parts)
            if re.match(r"^g-p-[0-9a-fA-F]{32}(?=-|$)", part)
        ),
        None,
    )
    if project_index is None:
        raise InvalidProjectUrl("Could not find the canonical project URL segment.")
    canonical_id = extract_project_id(path_parts[project_index])
    path = "/" + "/".join(
        [*path_parts[:project_index], canonical_id, "c", chat_id]
    )
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


_PROJECT_SAMPLE_SCRIPT = r"""
({ regions, specificRegions, links, names, specificNames, emptySelectors, emptyPattern,
   loadingSelectors, endSelectors, loadMoreSelectors }) => {
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
  const specificRegion = specificCandidates.find((node) => all(node, links).length > 0)
    || specificCandidates[0] || null;
  const region = specificRegion || candidates.find((node) => all(node, links).length > 0)
    || candidates[0] || null;
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
  const loading = all(document, loadingSelectors).some(visible);
  const explicitEnd = all(document, endSelectors).some(visible);
  const loadMore = all(document, loadMoreSelectors).some(visible);
  const totalCandidates = anchors.map((anchor) => Number(
    anchor.getAttribute("aria-setsize") || anchor.closest("[aria-setsize]")?.getAttribute("aria-setsize") || 0
  )).filter((value) => Number.isFinite(value) && value > 0);
  const totalCount = totalCandidates.length ? Math.max(...totalCandidates) : null;
  const recognized = Boolean(specificRegion || specificNameNode || chats.length || explicitEmpty);
  return { recognized, ready: Boolean(recognized && name && !loading), name, chats,
    projectScoped: Boolean(specificRegion && region === specificRegion),
    explicitEmpty, loading, explicitEnd, loadMore, totalCount };
}
"""


_PROJECT_SCROLL_SCRIPT = r"""
({ regions, loadMoreSelectors, linkSelectors }) => {
  // OUTOGPT_PROJECT_SCROLL
  const candidates = [];
  for (const selector of regions) {
    try { candidates.push(...document.querySelectorAll(selector)); } catch (_) {}
  }
  const scrollables = [];
  for (const root of candidates) {
    for (const node of [root, ...root.querySelectorAll("*")]) {
      if (node.scrollHeight > node.clientHeight + 1) {
        let linkCount = 0;
        for (const selector of linkSelectors) {
          try { linkCount += node.querySelectorAll(selector).length; } catch (_) {}
        }
        scrollables.push({ node, linkCount });
      }
    }
  }
  scrollables.sort((left, right) => right.linkCount - left.linkCount
    || right.node.scrollHeight - left.node.scrollHeight);
  const target = scrollables[0]?.node || null;
  const visible = (element) => {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden"
      && rect.width > 0 && rect.height > 0;
  };
  for (const selector of loadMoreSelectors) {
    try {
      const button = [...document.querySelectorAll(selector)].find(visible);
      if (button && !button.disabled) {
        button.click();
        return { found: true, moved: true, atEnd: false, loadMoreClicked: true };
      }
    } catch (_) {}
  }
  if (target) {
    const before = target.scrollTop;
    const maximum = Math.max(0, target.scrollHeight - target.clientHeight);
    target.scrollTop = Math.min(maximum, before + Math.max(target.clientHeight * 0.8, 1));
    target.dispatchEvent(new Event("scroll", { bubbles: true }));
    return { found: true, moved: target.scrollTop !== before,
      atEnd: target.scrollTop >= maximum - 1, before, after: target.scrollTop,
      scrollHeight: target.scrollHeight, clientHeight: target.clientHeight,
      loadMoreClicked: false };
  }
  const before = window.scrollY;
  const height = document.documentElement.scrollHeight;
  const maximum = Math.max(0, height - window.innerHeight);
  window.scrollTo(0, Math.min(maximum, before + Math.max(window.innerHeight * 0.8, 1)));
  return { found: true, moved: window.scrollY !== before,
    atEnd: window.scrollY >= maximum - 1, before, after: window.scrollY,
    scrollHeight: height, clientHeight: window.innerHeight, loadMoreClicked: false };
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
            "loadingSelectors": list(PROJECT_LOADING_INDICATORS),
            "endSelectors": list(PROJECT_END_INDICATORS),
            "loadMoreSelectors": list(PROJECT_LOAD_MORE_CONTROLS),
        },
    )


def _scroll_project_region(page: Any) -> Mapping[str, Any]:
    value = page.evaluate(
        _PROJECT_SCROLL_SCRIPT,
        {
            "regions": list(PROJECT_CONVERSATION_REGIONS),
            "loadMoreSelectors": list(PROJECT_LOAD_MORE_CONTROLS),
            "linkSelectors": list(PROJECT_CHAT_LINKS),
        },
    )
    if isinstance(value, Mapping):
        return value
    # Backward-compatible boundary for older adapters and deterministic fixtures.
    return {"found": True, "moved": bool(value), "atEnd": not bool(value)}


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
        raise ProjectAccessFailed("ChatGPT redirected away from the requested project.")


def discover_project_chats(
    page: Any,
    project_url: str,
    *,
    stable_rounds: int = 3,
    max_rounds: int = 400,
    poll_ms: int = DOM_POLL_MS,
    stall_rounds: int = 60,
    progress: ProjectProgress | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ProjectDiscovery:
    """Only a connected response chain ending in cursor:null proves completion."""
    if stable_rounds < 1 or max_rounds < 1 or stall_rounds < 1 or poll_ms < 0:
        raise ValueError("Polling limits must be positive (poll_ms may be zero).")
    project_url = validate_project_url(project_url)
    project_id = extract_project_id(project_url)
    discovered: dict[str, ProjectChat] = {}
    name = project_id
    with PaginationEvidence("project", project_id).start(page) as evidence:
        page.goto(
            project_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS
        )
        stable = 0
        previous = -1
        last_progress_round = 0
        last_progress_at = monotonic()
        last_revision = -1
        last_scroll_fingerprint = None
        last_scroll: Mapping[str, Any] = {}
        for round_index in range(max_rounds):
            _check_project_page_state(page, project_id, project_url)
            evidence.drain()
            sample = _project_sample(page)
            name = str(sample.get("name") or name)
            before = len(discovered)
            for payload in evidence.selected_project_pages():
                for item in payload["items"]:
                    identifier = item["id"]
                    discovered[identifier] = ProjectChat(
                        identifier,
                        project_chat_url(project_url, identifier),
                        str(item.get("title") or "Untitled conversation"),
                    )
            chain = evidence.chain()
            ready = (
                chain is not None
                and _project_ui_ready(sample)
                and not sample.get("loading")
            )
            stable = (
                stable + 1 if ready and previous == evidence.revision else int(ready)
            )
            previous = evidence.revision
            state = {
                **evidence.debug_state(),
                "round": round_index + 1,
                "chats": len(discovered),
                "complete_chain": chain is not None,
                "scroll": dict(last_scroll),
            }
            if progress is not None and (
                evidence.revision != last_revision or len(discovered) != before
            ):
                progress("project_discovery", state)
            if stable >= stable_rounds:
                if progress is not None:
                    progress("project_discovery_complete", state)
                return ProjectDiscovery(
                    project_id,
                    project_url,
                    name,
                    tuple(discovered.values()),
                    True,
                    "Verified initial request through cursor:null; all bodies parsed.",
                )
            last_scroll = _scroll_project_region(page)
            scroll_fingerprint = (
                last_scroll.get("after"),
                last_scroll.get("scrollHeight"),
                last_scroll.get("atEnd"),
                last_scroll.get("loadMoreClicked"),
            )
            progressed = (
                evidence.revision != last_revision
                or len(discovered) != before
                or bool(last_scroll.get("loadMoreClicked"))
                or (
                    bool(last_scroll.get("moved"))
                    and scroll_fingerprint != last_scroll_fingerprint
                )
            )
            if progressed:
                last_progress_round = round_index
                last_progress_at = monotonic()
            last_revision = evidence.revision
            last_scroll_fingerprint = scroll_fingerprint
            if round_index - last_progress_round >= stall_rounds:
                state = {
                    **evidence.debug_state(),
                    "round": round_index + 1,
                    "chats": len(discovered),
                    "last_progress_ms_ago": int(
                        max(0.0, monotonic() - last_progress_at) * 1000
                    ),
                    "scroll": dict(last_scroll),
                }
                if progress is not None:
                    progress("project_discovery_stalled", state)
                return ProjectDiscovery(
                    project_id,
                    project_url,
                    name,
                    tuple(discovered.values()),
                    False,
                    "Project pagination stalled before a verified terminal page: "
                    f"{state}",
                )
            page.wait_for_timeout(poll_ms)
        state = {
            **evidence.debug_state(),
            "round": max_rounds,
            "chats": len(discovered),
            "last_progress_ms_ago": int(
                max(0.0, monotonic() - last_progress_at) * 1000
            ),
            "scroll": dict(last_scroll),
        }
        if progress is not None:
            progress("project_discovery_limit", state)
        return ProjectDiscovery(
            project_id,
            project_url,
            name,
            tuple(discovered.values()),
            False,
            "Project pagination reached its scan limit without a verified terminal "
            f"page: {state}",
        )


def pair_messages(messages: Iterable[Mapping[str, Any]]) -> tuple[QAPair, ...]:
    """Pair logical turns, grouping consecutive assistant segments for one user."""
    pairs: list[QAPair] = []
    pending_user: str | None = None
    assistant_segments: list[str] = []
    for message in messages:
        role = str(message.get("role") or "")
        markdown = str(message.get("markdown") or "").strip()
        if role not in {"user", "assistant"} or not markdown:
            raise ConversationStructureError(
                "A conversation message had an invalid role or empty body."
            )
        if role == "user":
            if pending_user is not None:
                if not assistant_segments:
                    raise ConversationStructureError(
                        "Two user messages appeared without a safely pairable assistant response."
                    )
                pairs.append(QAPair(pending_user, "\n\n".join(assistant_segments)))
            pending_user = markdown
            assistant_segments = []
            continue
        if pending_user is None:
            raise ConversationStructureError(
                "An assistant message appeared without a preceding user message."
            )
        assistant_segments.append(markdown)
    if pending_user is not None and assistant_segments:
        pairs.append(QAPair(pending_user, "\n\n".join(assistant_segments)))
    # One trailing user message is intentionally ignored: it is not a complete QA pair.
    return tuple(pairs)


_CONVERSATION_SCRIPT = r"""
({ rootSelectors, turnSelectors, roleSelectors, auxiliarySelectors, attachmentSelectors, imageSelectors,
   titleSelectors, emptySelectors, loadingSelectors, exclusions }) => {
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
  const messageRole = (node) => {
    const explicit = String(node?.getAttribute?.("data-message-author-role")
      || node?.getAttribute?.("data-conversation-role") || "").toLowerCase();
    if (explicit === "user" || explicit === "assistant") return explicit;
    if (node?.matches?.("[data-user-message-bubble]")) return "user";
    if (node?.matches?.("[data-chatgpt-selection-message-id]")) return "assistant";
    if (node?.matches?.('[data-testid="chatgpt-writing-block"]')) return "assistant";
    if (node?.hasAttribute?.("data-turn-key")
        && query(node, attachmentSelectors).some((attachment) => !inactive(attachment, node))
        && !node.querySelector?.("[data-chatgpt-selection-message-id]")) return "user";
    return "";
  };
  const messageId = (turn, roleNode) => {
    const role = messageRole(roleNode);
    const direct = roleNode.getAttribute?.("data-message-id")
      || roleNode.getAttribute?.("data-chatgpt-selection-message-id")
      || roleNode.querySelector?.("[data-chatgpt-selection-message-id]")
        ?.getAttribute("data-chatgpt-selection-message-id");
    if (direct) return direct;
    if (role === "user") return turn.getAttribute?.("data-turn-key") || "";
    if (role === "assistant") {
      const candidates = turn.querySelectorAll?.("[data-chatgpt-selection-message-id]") || [];
      if (candidates.length === 1) {
        return candidates[0].getAttribute("data-chatgpt-selection-message-id") || "";
      }
    }
    return "";
  };
  const logicalRoleNodes = (turn) => {
    const roleNodes = [
      ...(matches(turn, roleSelectors) || messageRole(turn) ? [turn] : []),
      ...query(turn, roleSelectors)
    ].filter(
      (node) => !inactive(node, turn)
    );
    const outermost = roleNodes.filter((node) => !roleNodes.some(
      (other) => other !== node && other.contains(node)
    ));
    const unique = [];
    const seen = new Set();
    for (const node of outermost.sort((left, right) => depthFromTurn(left, turn) - depthFromTurn(right, turn))) {
      const id = messageId(turn, node) || node.getAttribute("data-turn-id") || "";
      const key = id ? `id:${id}` : `${messageRole(node)}:${clean(node.textContent)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      unique.push(node);
    }
    return unique;
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
    if (messageRole(roleNode) !== "user") return [];
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
  const stableTurnId = (turn, roleNode, index, count) => {
    for (const [attribute, node] of [
      ["data-message-id", roleNode],
      ["data-turn-id", roleNode],
      ["data-turn-id", turn],
      ["data-message-id", turn],
      ["data-chatgpt-selection-message-id", roleNode],
      ["data-turn-key", turn],
      ["data-testid", turn]
    ]) {
      const value = String(node.getAttribute?.(attribute) || "").trim();
      if (value) return `${attribute}:${value}${count > 1 ? `:${index}` : ""}`;
    }
    return "";
  };
  let invalidTurns = 0;
  const invalidTurnDetails = [];
  let auxiliaryTurns = 0;
  const messages = turns.flatMap((turn) => {
    const roleNodes = logicalRoleNodes(turn);
    if (!roleNodes.length) {
      if (query(turn, auxiliarySelectors).some((node) => !inactive(node, turn))) {
        auxiliaryTurns += 1;
        return [];
      }
      const clone = turn.cloneNode(true);
      for (const selector of exclusions) {
        try { clone.querySelectorAll(selector).forEach((node) => node.remove()); } catch (_) {}
      }
      const leftover = clean(clone.textContent);
      if (leftover) {
        invalidTurns += 1;
        invalidTurnDetails.push({
          turnId: String(turn.getAttribute("data-turn-id")
            || turn.getAttribute("data-message-id") || turn.getAttribute("data-testid") || ""),
          text: leftover.slice(0, 160)
        });
      }
      else auxiliaryTurns += 1;
      return [];
    }
    return roleNodes.map((roleNode, index) => {
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
        turnId: stableTurnId(turn, roleNode, index, roleNodes.length),
        turnKey: String(turn.getAttribute?.("data-turn-key") || ""),
        messageId: messageId(turn, roleNode),
        role: messageRole(roleNode),
        markdown: clean(render(content)),
        attachments
      };
    });
  });
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
  const loading = query(document, loadingSelectors).some((node) => !inactive(node));
  return {
    recognized: turns.length > 0 || explicitEmpty,
    explicitEmpty,
    title,
    turnCount: turns.length,
    invalidTurns,
    invalidTurnDetails,
    auxiliaryTurns,
    loading,
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
  const style = getComputedStyle(container);
  const reverse = String(style.flexDirection || "").toLowerCase() === "column-reverse";
  const extent = Math.max(0, Number(container.scrollHeight || 0) - Number(container.clientHeight || 0));
  const step = Math.max(Number(container.clientHeight || 0) * 0.8, 1);
  const target = reverse ? Math.max(-extent, before - step) : Math.max(0, before - step);
  try { container.scrollTo({ top: target, behavior: "instant" }); }
  catch (_) { container.scrollTop = target; }
  container.scrollTop = target;
  try { container.dispatchEvent(new Event("scroll", { bubbles: true })); } catch (_) {}
  const after = Number(container.scrollTop || 0);
  const wasAtTop = reverse ? before <= -extent + 1 : before <= 1;
  const atTop = reverse ? after <= -extent + 1 : after <= 1;
  return {
    found: true,
    containerKind,
    before,
    after,
    reverse,
    wasAtTop,
    atTop,
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
            "auxiliarySelectors": list(MESSAGE_AUXILIARY_NODES),
            "attachmentSelectors": list(MESSAGE_ATTACHMENT_NODES),
            "imageSelectors": list(MESSAGE_ATTACHMENT_IMAGES),
            "titleSelectors": list(CONVERSATION_TITLES),
            "emptySelectors": list(CONVERSATION_EMPTY_STATES),
            "loadingSelectors": list(CONVERSATION_LOADING_INDICATORS),
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
) -> tuple[dict[str, Any], ...]:
    """Add placeholders only when the DOM reported concrete attachment evidence."""
    normalized: list[dict[str, Any]] = []
    seen_ids: dict[str, tuple[str, str]] = {}
    for message in messages:
        if message.get("auxiliary"):
            continue
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
        turn_id = str(message.get("turnId") or "").strip()
        identity = (role, markdown)
        if turn_id and seen_ids.get(turn_id) == identity:
            continue
        if turn_id and turn_id in seen_ids and seen_ids[turn_id] != identity:
            raise ConversationStructureError(
                f"Conversation message identity {turn_id!r} was rendered with conflicting content."
            )
        if (
            normalized
            and not turn_id
            and not normalized[-1]["turnId"]
            and (normalized[-1]["role"], normalized[-1]["markdown"]) == identity
        ):
            continue
        if turn_id:
            seen_ids[turn_id] = identity
        normalized.append(
            {
                "turnId": turn_id,
                "turnKey": str(message.get("turnKey") or ""),
                "messageId": str(message.get("messageId") or ""),
                "role": role,
                "markdown": markdown,
                "attachments": list(message.get("attachments") or ()),
            }
        )
    return tuple(normalized)


def _verify_network_messages(evidence, accumulated, observed_turn_keys=()):
    nodes = evidence.ordered_nodes()
    if nodes is None:
        evidence.verification_wait = "network cursor chain is incomplete"
        return None
    evidence.verification_wait = None

    def waiting(reason):
        evidence.verification_wait = reason
        return None

    visible = []
    hidden = []
    network_ids = set()

    def source_markdown(source_text, dom):
        additions = []
        for attachment in dom.get("attachments") or ():
            if attachment.get("kind") == "image":
                placeholder = "[Image attachment]"
            elif attachment.get("kind") == "file":
                name = str(attachment.get("name") or "").replace("[", r"\[").replace(
                    "]", r"\]"
                )
                placeholder = f"[Attachment: {name}]" if name else "[Attachment]"
            else:
                continue
            if placeholder not in additions:
                additions.append(placeholder)
        return "\n\n".join(part for part in (source_text, *additions) if part)

    for node_index, node in enumerate(nodes):
        message = node.get("message")
        if message is None:
            continue  # Structural mapping node, not a message.
        identifier = message["id"]
        network_ids.add(identifier)
        role = message["author"].get("role")
        metadata = message.get("metadata") or {}
        channel = message.get("channel") or metadata.get("channel")
        content = message.get("content") or {}
        parts = content.get("parts")
        model_context = (
            role == "assistant"
            and content.get("content_type") == "model_editable_context"
            and (parts is None or parts == [])
            and message.get("recipient") == "all"
        )
        internal_tool_call = (
            role == "assistant"
            and isinstance(message.get("recipient"), str)
            and message.get("recipient") not in {"", "all"}
            and content.get("content_type") in {"code", "text"}
            and message.get("end_turn") in {None, False}
        )
        internal_thoughts = (
            role == "assistant"
            and content.get("content_type") in {"thoughts", "reasoning_recap"}
            and (parts is None or parts == [])
            and message.get("recipient") == "all"
            and message.get("end_turn") is False
        )
        intermediate_assistant = role == "assistant" and message.get("end_turn") is False
        empty_user_placeholder = (
            role == "user"
            and content.get("content_type") == "text"
            and isinstance(parts, list)
            and all(isinstance(part, str) and not part.strip() for part in parts)
            and not metadata.get("attachments")
        )
        non_ui = (
            role in {"system", "tool", "developer"}
            or (role == "assistant" and channel in {"analysis", "commentary"})
            or model_context
            or internal_tool_call
            or internal_thoughts
            or intermediate_assistant
            or empty_user_placeholder
            or metadata.get("is_visually_hidden_from_conversation") is True
        )
        if role == "assistant" and message.get("status") != "finished_successfully":
            historical_internal = non_ui and node_index < len(nodes) - 1
            if not historical_internal:
                evidence.fail(
                    f"Assistant message {identifier} is not confirmed finished."
                )
        if non_ui:
            hidden.append(
                {
                    "id": identifier,
                    "reason": (
                        "non-UI role, internal channel/context/tool stage, empty "
                        "placeholder, or explicit hidden metadata"
                    ),
                    "source": message,
                }
            )
            continue
        if role not in {"user", "assistant"}:
            evidence.fail(f"Unclassified message role for {identifier}.")
        dom = accumulated.get(identifier)
        if dom is None and role == "user" and identifier in observed_turn_keys:
            blank_parts = (
                isinstance(parts, list)
                and all(isinstance(part, str) and not part.strip() for part in parts)
            )
            merged_files = metadata.get("attachments") or []
            if (
                blank_parts
                and isinstance(merged_files, list)
                and merged_files
                and all(
                    isinstance(item, dict) and isinstance(item.get("name"), str)
                    and item["name"]
                    for item in merged_files
                )
            ):
                attachments = [
                    {"kind": "file", "name": item["name"]} for item in merged_files
                ]
                dom = {
                    "turnId": f"data-turn-key:{identifier}",
                    "turnKey": identifier,
                    "messageId": identifier,
                    "role": "user",
                    "markdown": "\n\n".join(
                        f"[Attachment: {item['name']}]" for item in merged_files
                    ),
                    "attachments": attachments,
                }
        if dom is None or dom["role"] != role or not dom["markdown"]:
            parts = parts or []
            source_characters = sum(len(part) for part in parts if isinstance(part, str))
            diagnostic = {
                "role": role,
                "status": message.get("status"),
                "channel": channel,
                "recipient": message.get("recipient"),
                "end_turn": message.get("end_turn"),
                "content_type": content.get("content_type"),
                "parts": len(parts) if isinstance(parts, list) else None,
                "source_chars": source_characters,
                "hidden": metadata.get("is_visually_hidden_from_conversation"),
                "message_type": metadata.get("message_type"),
                "real_author": metadata.get("real_author"),
            }
            missing = []
            for candidate_node in nodes:
                candidate = candidate_node.get("message")
                if not candidate or candidate.get("id") in accumulated:
                    continue
                candidate_content = candidate.get("content") or {}
                candidate_parts = candidate_content.get("parts") or []
                candidate_metadata = candidate.get("metadata") or {}
                missing.append(
                    {
                        "role": (candidate.get("author") or {}).get("role"),
                        "status": candidate.get("status"),
                        "channel": candidate.get("channel")
                        or candidate_metadata.get("channel"),
                        "recipient": candidate.get("recipient"),
                        "end_turn": candidate.get("end_turn"),
                        "content_type": candidate_content.get("content_type"),
                        "parts": len(candidate_parts)
                        if isinstance(candidate_parts, list)
                        else None,
                        "source_chars": sum(
                            len(part)
                            for part in candidate_parts
                            if isinstance(part, str)
                        ),
                        "hidden": candidate_metadata.get(
                            "is_visually_hidden_from_conversation"
                        ),
                        "message_type": candidate_metadata.get("message_type"),
                    }
                )
            omitted = max(0, len(missing) - 12)
            return waiting(
                "visible message is not accumulated from the DOM: "
                f"{diagnostic}; missing_sample={missing[:12]}; omitted={omitted}"
            )
        content = message["content"]
        parts = content.get("parts")
        if content.get("content_type") not in {
            "text",
            "multimodal_text",
        } or not isinstance(parts, list):
            # Preserve source, but an unknown UI representation is not completion evidence.
            evidence.fail(
                f"Message {identifier} content type requires explicit UI verification support."
            )
        images = [part for part in parts if isinstance(part, dict)]
        if any(not isinstance(part, (str, dict)) for part in parts) or any(
            part.get("content_type") != "image_asset_pointer"
            or not part.get("asset_pointer")
            for part in images
        ):
            evidence.fail(f"Message {identifier} has unclassified content parts.")
        attachments = dom.get("attachments") or []
        if len([item for item in attachments if item.get("kind") == "image"]) != len(
            images
        ):
            return waiting("image attachment evidence is not synchronized")
        files = metadata.get("attachments") or []
        if not isinstance(files, list) or any(
            not isinstance(item, dict) or not item.get("name") for item in files
        ):
            evidence.fail(f"Message {identifier} has unclassified file attachments.")
        if sorted(item["name"] for item in files) != sorted(
            item.get("name", "") for item in attachments if item.get("kind") == "file"
        ):
            return waiting("file attachment evidence is not synchronized")
        source_text = "\n\n".join(
            part for part in parts if isinstance(part, str)
        ).strip()
        rendered_text = dom["markdown"]
        if images:
            rendered_text = _IMAGE_MARKDOWN.sub("", rendered_text).replace(
                "[Image attachment]", ""
            )
        for item in files:
            name = str(item["name"]).replace("[", r"\[").replace("]", r"\]")
            rendered_text = rendered_text.replace(f"[Attachment: {name}]", "")

        # Markdown is generated by the existing DOM converter; compare text tokens
        # after formatting/whitespace normalization, and retain exact source as well.
        def canonical(value):
            return re.sub(r"[\s`*_#>|\\]+", "", value)

        source_canonical = canonical(source_text)
        rendered_canonical = canonical(rendered_text)
        archived_markdown = dom["markdown"]
        if source_canonical != rendered_canonical:
            source_semantic = "".join(
                character.casefold()
                for character in source_text
                if character.isalnum()
            )
            rendered_semantic = "".join(
                character.casefold()
                for character in rendered_text
                if character.isalnum()
            )
            matcher = SequenceMatcher(None, source_semantic, rendered_semantic)
            matched = sum(block.size for block in matcher.get_matching_blocks())
            source_coverage = matched / max(1, len(source_semantic))
            rendered_coverage = matched / max(1, len(rendered_semantic))
            if not (
                len(source_semantic) >= 128
                and source_coverage >= 0.80
                and rendered_coverage >= 0.95
            ):
                return waiting(
                    "network source text and rendered DOM text differ "
                    f"(role={role}, channel={channel}, parts={len(parts)}, "
                    f"source_chars={len(source_canonical)}, "
                    f"rendered_chars={len(rendered_canonical)}, "
                    f"source_coverage={source_coverage:.3f}, "
                    f"rendered_coverage={rendered_coverage:.3f})"
                )
            # The UI can collapse citation or long-answer segments. The exact
            # UUID and ordered DOM coverage verify the visible message, while
            # the response source prevents collapsed text from being lost.
            archived_markdown = source_markdown(source_text, dom)
        visible.append(
            {**dom, "markdown": archived_markdown, "id": identifier, "source": message}
        )
    if set(accumulated) - network_ids:
        evidence.fail(
            "DOM contains message UUIDs absent from the completed response chain."
        )
    # A branched mapping cannot safely be linearized as QA. Require a single
    # visible ancestry; explicitly hidden/tool messages may occur between turns.
    by_id = {node["id"]: node for node in nodes}
    previous = None
    for message in visible:
        parent = by_id[message["id"]]["parent"]
        # Stop at the prior visible message: linear even for very long histories.
        while parent is not None and parent != previous:
            parent = by_id[parent]["parent"]
        if previous is not None and parent != previous:
            evidence.fail(
                "Visible message branches cannot be verified as one conversation sequence."
            )
        previous = message["id"]
    return tuple(visible), tuple(hidden)


def _hydrate_conversation_history(
    page,
    evidence,
    *,
    stable_rounds,
    max_rounds,
    poll_ms,
    stall_rounds,
    progress,
    monotonic,
):
    accumulated: dict[str, dict] = {}
    observed_turn_keys: set[str] = set()
    previous = None
    stable = 0
    saw_generation = False
    sample = {}
    last_progress_round = 0
    last_progress_at = monotonic()
    last_progress_token = None
    last_scroll: Mapping[str, Any] = {}
    last_scroll_fingerprint = None
    for round_index in range(max_rounds):
        if login_or_challenge_visible(page) or "/auth/" in getattr(page, "url", ""):
            raise LoginRequired(
                "ChatGPT authentication is required to read the conversation."
            )
        evidence.drain()
        if generation_in_progress(page):
            saw_generation = True
            stable = 0
            if progress is not None and round_index == 0:
                progress(
                    "conversation_generation",
                    {**evidence.debug_state(), "round": round_index + 1},
                )
            page.wait_for_timeout(poll_ms)
            continue
        if saw_generation:
            # The initial GET may contain a partial response. Restart this same
            # chat after generation stops to obtain fresh authoritative evidence.
            raise ConversationLoadingUnknown(
                "Generation stopped; reload this chat to verify the final response."
            )
        sample = _conversation_sample(page)
        messages = _normalize_conversation_messages(sample.get("messages") or ())
        observed_turn_keys.update(
            message["turnKey"] for message in messages if message.get("turnKey")
        )
        window = {}
        for message in messages:
            identifier = message["messageId"]
            if not identifier:
                raise ConversationHistoryIncomplete(
                    "DOM message has no network-comparable message UUID."
                )
            if identifier in window and window[identifier] != message:
                raise ConversationHistoryIncomplete(
                    "DOM window has conflicting content for one message UUID."
                )
            window[identifier] = message
            accumulated[identifier] = message
        verified = _verify_network_messages(
            evidence, accumulated, observed_turn_keys=observed_turn_keys
        )
        fingerprint = (evidence.revision, repr(verified))
        ready = (
            verified is not None
            and not sample.get("loading")
            and not sample.get("invalidTurns")
        )
        stable = stable + 1 if ready and fingerprint == previous else int(ready)
        previous = fingerprint
        progress_token = (
            evidence.revision,
            len(accumulated),
            tuple(
                (message.get("messageId"), message.get("markdown"))
                for message in messages
            ),
        )
        if progress_token != last_progress_token:
            last_progress_round = round_index
            last_progress_at = monotonic()
            last_progress_token = progress_token
            if progress is not None:
                progress(
                    "conversation_loading",
                    {
                        **evidence.debug_state(),
                        "round": round_index + 1,
                        "dom_messages": len(accumulated),
                        "verified": verified is not None,
                    },
                )
        if (
            stable >= stable_rounds
            and last_scroll.get("found")
            and last_scroll.get("atTop")
        ):
            if progress is not None:
                progress(
                    "conversation_complete",
                    {
                        **evidence.debug_state(),
                        "round": round_index + 1,
                        "dom_messages": len(accumulated),
                        "scroll": dict(last_scroll),
                    },
                )
            return {**sample, "messages": verified[0], "non_ui_messages": verified[1]}
        last_scroll = _scroll_conversation_history_to_top(page)
        scroll_fingerprint = (
            last_scroll.get("before"),
            last_scroll.get("after"),
            last_scroll.get("scrollHeight"),
            last_scroll.get("atTop"),
        )
        if (
            last_scroll.get("after") != last_scroll.get("before")
            and scroll_fingerprint != last_scroll_fingerprint
        ):
            last_progress_round = round_index
            last_progress_at = monotonic()
            if progress is not None and round_index % 20 == 0:
                progress(
                    "conversation_scrolling",
                    {
                        **evidence.debug_state(),
                        "round": round_index + 1,
                        "dom_messages": len(accumulated),
                        "scroll": dict(last_scroll),
                    },
                )
        last_scroll_fingerprint = scroll_fingerprint
        if round_index - last_progress_round >= stall_rounds:
            state = {
                **evidence.debug_state(),
                "round": round_index + 1,
                "dom_messages": len(accumulated),
                "last_progress_ms_ago": int(
                    max(0.0, monotonic() - last_progress_at) * 1000
                ),
                "scroll": dict(last_scroll),
            }
            if progress is not None:
                progress("conversation_stalled", state)
            raise ConversationHistoryIncomplete(
                "Conversation loading stalled before all network pages and DOM "
                f"messages were verified: {state}"
            )
        page.wait_for_timeout(poll_ms)
    raise ConversationHistoryIncomplete(
        "Conversation remains pending: terminal page, pending bodies, generation, or message content not verified."
    )


def read_conversation(
    page: Any,
    chat: ProjectChat,
    *,
    stable_rounds: int = 3,
    max_rounds: int = 400,
    poll_ms: int = DOM_POLL_MS,
    stall_rounds: int = 60,
    progress: ProjectProgress | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ConversationSnapshot:
    """Install CDP monitoring before navigation; no DOM-only completion fallback."""
    if stable_rounds < 1 or max_rounds < 1 or stall_rounds < 1 or poll_ms < 0:
        raise ValueError("Polling limits must be positive (poll_ms may be zero).")
    with PaginationEvidence("conversation", chat.chat_id).start(page) as evidence:
        try:
            page.goto(
                chat.chat_url,
                wait_until="domcontentloaded",
                timeout=PAGE_LOAD_TIMEOUT_MS,
            )
        except Exception as exc:
            raise ConversationLoadingUnknown(
                f"Could not load conversation {chat.chat_id}: {exc}"
            ) from exc
        if login_or_challenge_visible(page) or "/auth/" in getattr(page, "url", ""):
            raise LoginRequired(
                "ChatGPT authentication is required to read the conversation."
            )
        try:
            loaded_url = validate_chat_url(getattr(page, "url", chat.chat_url))
        except InvalidChatUrl as exc:
            raise ProjectAccessFailed(
                "Conversation redirected away from its page."
            ) from exc
        if extract_chat_id(loaded_url) != chat.chat_id:
            raise ProjectAccessFailed(
                "Conversation redirected to a different conversation."
            )
        complete = _hydrate_conversation_history(
            page,
            evidence,
            stable_rounds=stable_rounds,
            max_rounds=max_rounds,
            poll_ms=poll_ms,
            stall_rounds=stall_rounds,
            progress=progress,
            monotonic=monotonic,
        )
        messages = complete["messages"]
        return ConversationSnapshot(
            chat.chat_id,
            loaded_url,
            str(complete.get("title") or chat.title),
            pair_messages(messages),
            messages=messages,
            non_ui_messages=complete["non_ui_messages"],
        )
