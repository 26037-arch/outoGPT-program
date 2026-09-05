export const MESSAGE_TYPES = Object.freeze({
  REGISTER_AGENT: "REGISTER_AGENT",
  DETECT_PROJECT: "DETECT_PROJECT",
  CHAT_STATE: "CHAT_STATE",
  RESPONSE_STARTED: "RESPONSE_STARTED",
  RESPONSE_COMPLETED: "RESPONSE_COMPLETED",
  SAVE_CONVERSATION: "SAVE_CONVERSATION",
  SAVE_COMPLETED: "SAVE_COMPLETED",
  AGENT_ERROR: "AGENT_ERROR",
  GET_STATE: "GET_STATE",
  SAVE_MARKDOWN: "SAVE_MARKDOWN",
  EXPORT_MARKDOWN: "EXPORT_MARKDOWN",
  SEND_PROMPT: "SEND_PROMPT"
});

export function makeAgentMessage(type, fields = {}) {
  return { type, protocolVersion: 1, ...fields };
}

export function isProtocolMessage(value) {
  return Boolean(value && typeof value === "object" && typeof value.type === "string");
}

