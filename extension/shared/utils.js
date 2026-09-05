const RESERVED_WINDOWS_NAMES = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i;

export function sanitizeFileName(value, fallback = "Untitled conversation") {
  let result = String(value ?? "")
    .normalize("NFC")
    .replace(/[<>:"/\\|?*\u0000-\u001F]/g, "-")
    .replace(/\s+/g, " ")
    .replace(/[. ]+$/g, "")
    .trim();
  if (!result) result = fallback;
  if (RESERVED_WINDOWS_NAMES.test(result)) result = `_${result}`;
  return result.slice(0, 120).replace(/[. ]+$/g, "") || fallback;
}

export function extractProjectId(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== "chatgpt.com") return null;
    return parsed.pathname.split("/").filter(Boolean).find((part) => part.startsWith("g-p-")) ?? null;
  } catch {
    return null;
  }
}

export function extractConversationId(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== "chatgpt.com") return null;
    const parts = parsed.pathname.split("/").filter(Boolean);
    const index = parts.indexOf("c");
    return index >= 0 && parts[index + 1] ? decodeURIComponent(parts[index + 1]) : null;
  } catch {
    return null;
  }
}

export function validateProjectUrl(url) {
  const projectId = extractProjectId(url);
  if (!projectId || extractConversationId(url)) {
    throw new Error("Enter a ChatGPT Project page URL, not a conversation URL.");
  }
  return { projectUrl: new URL(url).href, projectId };
}

export function stableHash(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

export function shortId(value, length = 6) {
  return String(value ?? "").replace(/[^a-zA-Z0-9]/g, "").slice(0, length) || stableHash(value).slice(0, length);
}

export function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
