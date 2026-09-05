import { ERROR_CODES } from "../shared/constants.js";
import { isProtocolMessage, makeAgentMessage, MESSAGE_TYPES } from "../shared/messages.js";
import { LatestWriteQueue, writeConversationSnapshot } from "../storage/filesystem.js";
import { getRootHandle } from "../storage/handle-store.js";
import { getSettings, setLastStatus } from "../storage/settings.js";

const tabStates = new Map();
const writeQueue = new LatestWriteQueue();

chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());

async function route(message, sender) {
  if (!isProtocolMessage(message)) return undefined;
  const tabId = sender.tab?.id ?? message.tabId ?? null;

  if (message.type === MESSAGE_TYPES.REGISTER_AGENT) {
    return { ok: true, tabId, settings: await getSettings() };
  }

  if (message.type === MESSAGE_TYPES.CHAT_STATE || message.type === MESSAGE_TYPES.RESPONSE_STARTED || message.type === MESSAGE_TYPES.RESPONSE_COMPLETED) {
    const state = { ...message, tabId };
    tabStates.set(tabId, state);
    await setLastStatus(state);
    return { ok: true };
  }

  if (message.type === MESSAGE_TYPES.GET_STATE) {
    return { ok: true, states: [...tabStates.values()] };
  }

  if (message.type === MESSAGE_TYPES.AGENT_ERROR) {
    const status = { ...message, tabId };
    tabStates.set(tabId, status);
    await setLastStatus(status);
    return { ok: true };
  }

  if (message.type === MESSAGE_TYPES.SAVE_CONVERSATION) {
    const settings = await getSettings();
    if (!settings.initialized || message.snapshot?.projectId !== settings.projectId) {
      const error = { ok: false, error: ERROR_CODES.PROJECT_NOT_REGISTERED, detail: "Conversation is not in the registered project." };
      await setLastStatus({ ...error, tabId, conversationId: message.conversationId });
      return error;
    }
    const rootHandle = await getRootHandle();
    try {
      const result = await writeQueue.enqueue(message.conversationId, () =>
        writeConversationSnapshot(rootHandle, message.snapshot)
      );
      const response = makeAgentMessage(MESSAGE_TYPES.SAVE_COMPLETED, { ok: true, tabId, ...result });
      await setLastStatus(response);
      return response;
    } catch (cause) {
      const response = {
        ok: false,
        tabId,
        conversationId: message.conversationId,
        error: cause?.code || ERROR_CODES.MARKDOWN_WRITE_ERROR,
        detail: cause?.message ?? String(cause)
      };
      await setLastStatus(response);
      return response;
    }
  }

  if ([MESSAGE_TYPES.SEND_PROMPT, MESSAGE_TYPES.EXPORT_MARKDOWN].includes(message.type)) {
    return { ok: false, error: `${message.type} is reserved for a future controller and is not implemented.` };
  }
  return undefined;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  route(message, sender).then(sendResponse).catch((error) => {
    console.error("[outoGPT:router] message failed", error);
    sendResponse({ ok: false, error: error?.message ?? String(error) });
  });
  return true;
});

chrome.tabs.onRemoved.addListener((tabId) => tabStates.delete(tabId));

