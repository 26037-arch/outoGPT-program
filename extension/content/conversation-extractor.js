import { cloneChatGptMessageContent } from "../markdown/chatgpt-rules.js";
import { nodeToMarkdown } from "../markdown/converter.js";
import { findConversationTitle, findMessageElements, getRoleForMessage } from "./selectors.js";
import { extractConversationId, extractProjectId, stableHash } from "../shared/utils.js";

export function extractConversation({ projectName, root = document, url = location.href } = {}) {
  const conversationId = extractConversationId(url);
  const projectId = extractProjectId(url);
  if (!conversationId || !projectId) return null;

  const messages = [];
  for (const element of findMessageElements(root)) {
    const role = getRoleForMessage(element);
    if (!role) continue;
    const content = cloneChatGptMessageContent(element);
    const markdown = nodeToMarkdown(content).replace(/\n{3,}/g, "\n\n").trim();
    if (markdown) messages.push({ role, markdown });
  }

  const title = findConversationTitle(root) || "Untitled conversation";
  const snapshot = { conversationId, projectId, projectName, title, url, messages };
  return { ...snapshot, contentHash: stableHash(snapshot) };
}

