export function cloneChatGptMessageContent(messageElement) {
  const roleNode = messageElement.matches?.("[data-message-author-role]")
    ? messageElement
    : messageElement.querySelector?.("[data-message-author-role]") ?? messageElement;
  const clone = roleNode.cloneNode(true);
  for (const unwanted of clone.querySelectorAll(
    'button, svg, [data-testid*="copy" i], [data-testid*="feedback" i], [aria-hidden="true"]'
  )) {
    unwanted.remove();
  }
  return clone.querySelector?.(".markdown") ?? clone;
}

export function extractMath(element) {
  const isMathContainer = element.matches?.(
    ".katex, .katex-display, [data-math], [data-math-display='true'], math"
  );
  if (!isMathContainer) return null;

  const annotation = element.querySelector?.('annotation[encoding="application/x-tex"]');
  const source = annotation?.textContent?.trim() || element.getAttribute?.("data-math")?.trim();
  if (!source) return null;
  const display = element.matches?.(".katex-display, [data-math-display='true']")
    || element.closest?.(".katex-display");
  return display ? `\n\n$$\n${source}\n$$\n\n` : `$${source}$`;
}

export function extractCodeLanguage(codeElement) {
  const classes = String(codeElement?.className ?? "").split(/\s+/);
  const languageClass = classes.find((name) => name.startsWith("language-"));
  if (languageClass) return languageClass.slice("language-".length);
  const wrapperLanguage = codeElement?.closest?.("[data-language]")?.getAttribute("data-language");
  return wrapperLanguage || "";
}
