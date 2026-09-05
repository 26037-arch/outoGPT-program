export const SELECTORS = Object.freeze({
  stopControls: [
    'button[data-testid="stop-button"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Stop streaming"]',
    'button[aria-label*="생성 중지"]',
    'button[aria-label*="응답 중지"]'
  ],
  messageRoots: [
    '[data-message-author-role="user"]',
    '[data-message-author-role="assistant"]'
  ],
  conversationTitles: [
    '[data-testid="conversation-title"]',
    'nav [aria-current="page"]',
    'main h1'
  ],
  projectNames: [
    '[data-testid="project-name"]',
    '[aria-label*="Project"] h1',
    'main h1'
  ]
});

function isVisible(element) {
  if (!(element instanceof Element)) return false;
  const style = getComputedStyle(element);
  const rectangle = element.getBoundingClientRect();
  return style.visibility !== "hidden" && style.display !== "none" && rectangle.width > 0 && rectangle.height > 0;
}

export function firstVisible(selectors, root = document) {
  for (const selector of selectors) {
    let elements;
    try {
      elements = root.querySelectorAll(selector);
    } catch (error) {
      console.warn(`[outoGPT] Invalid selector: ${selector}`, error);
      continue;
    }
    for (const element of elements) {
      if (isVisible(element)) return { element, selector };
    }
  }
  return null;
}

export function findStopControl(root = document) {
  const semantic = firstVisible(SELECTORS.stopControls, root);
  if (semantic) return semantic;

  const accessiblePattern = /^(stop (generating|streaming)|생성 중지|응답 중지)$/i;
  for (const button of root.querySelectorAll('button, [role="button"]')) {
    const name = (button.getAttribute("aria-label") || button.textContent || "").trim();
    if (accessiblePattern.test(name) && isVisible(button)) {
      return { element: button, selector: "accessible-name-fallback" };
    }
  }
  return null;
}

export function hasStopControl(root = document) {
  return findStopControl(root) !== null;
}

export function findMessageElements(root = document) {
  const primary = [...root.querySelectorAll('[data-message-author-role="user"], [data-message-author-role="assistant"]')];
  if (primary.length) return primary;
  return [...root.querySelectorAll('article[data-testid^="conversation-turn"]')]
    .filter((element) => element.querySelector('[data-message-author-role="user"], [data-message-author-role="assistant"]'));
}

export function getRoleForMessage(element) {
  const roleNode = element.matches?.("[data-message-author-role]")
    ? element
    : element.querySelector?.("[data-message-author-role]");
  const role = roleNode?.getAttribute("data-message-author-role");
  return role === "user" || role === "assistant" ? role : null;
}

export function findConversationTitle(root = document) {
  const match = firstVisible(SELECTORS.conversationTitles, root);
  const text = match?.element?.textContent?.trim();
  if (text && text.length <= 300) return text;
  const documentTitle = root.title?.replace(/\s*[|–-]\s*ChatGPT\s*$/i, "").trim();
  return documentTitle && documentTitle.toLowerCase() !== "chatgpt" ? documentTitle : null;
}

export function findProjectNameCandidate(root = document) {
  const match = firstVisible(SELECTORS.projectNames, root);
  const text = match?.element?.textContent?.trim();
  if (text && text.length <= 200 && !/^chatgpt$/i.test(text)) {
    return { name: text, selector: match.selector };
  }
  const documentTitle = root.title?.replace(/\s*[|–-]\s*ChatGPT\s*$/i, "").trim();
  if (documentTitle && !/^chatgpt$/i.test(documentTitle)) {
    return { name: documentTitle, selector: "document.title" };
  }
  return null;
}

