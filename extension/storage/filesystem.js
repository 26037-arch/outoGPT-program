import { renderConversationMarkdown } from "../markdown/converter.js";
import { ERROR_CODES } from "../shared/constants.js";
import { sanitizeFileName, shortId } from "../shared/utils.js";
import { getSettings, mutateSettings } from "./settings.js";
import { queryWritePermission } from "./handle-store.js";

let filesystemTail = Promise.resolve();

export class LatestWriteQueue {
  constructor() {
    this.entries = new Map();
  }

  enqueue(key, task) {
    return new Promise((resolve, reject) => {
      const item = { task, resolve, reject };
      const existing = this.entries.get(key);
      if (!existing) {
        const entry = { pending: null };
        this.entries.set(key, entry);
        this.#drain(key, entry, item);
        return;
      }
      if (existing.pending) existing.pending.resolve({ status: "superseded" });
      existing.pending = item;
    });
  }

  async #drain(key, entry, first) {
    let current = first;
    while (current) {
      try {
        current.resolve(await current.task());
      } catch (error) {
        current.reject(error);
      }
      current = entry.pending;
      entry.pending = null;
    }
    this.entries.delete(key);
  }
}

async function fileExists(directory, name) {
  try {
    await directory.getFileHandle(name, { create: false });
    return true;
  } catch (error) {
    if (error?.name === "NotFoundError") return false;
    throw error;
  }
}

async function chooseFileName(projectDirectory, snapshot, settings) {
  const previous = settings.conversationFiles?.[snapshot.conversationId];
  if (previous?.fileName && previous.title === snapshot.title) return previous.fileName;

  const base = sanitizeFileName(snapshot.title);
  const readable = `${base}.md`;
  const claimed = Object.entries(settings.conversationFiles ?? {}).some(
    ([conversationId, metadata]) => conversationId !== snapshot.conversationId && metadata.fileName === readable
  );
  if (!claimed && !(await fileExists(projectDirectory, readable))) return readable;
  return `${base} -- ${shortId(snapshot.conversationId)}.md`;
}

export async function ensureProjectDirectory(rootHandle, projectName) {
  const chatGptDirectory = await rootHandle.getDirectoryHandle("ChatGPT", { create: true });
  return chatGptDirectory.getDirectoryHandle(sanitizeFileName(projectName, "ChatGPT Project"), { create: true });
}

async function performConversationWrite(rootHandle, snapshot) {
  const permission = await queryWritePermission(rootHandle);
  if (permission !== "granted") {
    const error = new Error("The selected folder needs read/write permission from the setup page.");
    error.code = ERROR_CODES.FILESYSTEM_PERMISSION_ERROR;
    throw error;
  }

  const settings = await getSettings();
  const existing = settings.conversationFiles?.[snapshot.conversationId];
  if (existing?.contentHash === snapshot.contentHash) {
    return { status: "unchanged", path: existing.path, conversationId: snapshot.conversationId };
  }

  try {
    const projectDirectory = await ensureProjectDirectory(rootHandle, snapshot.projectName);
    const fileName = await chooseFileName(projectDirectory, snapshot, settings);
    const updatedAt = new Date().toISOString();
    const markdown = renderConversationMarkdown(snapshot, updatedAt);
    const fileHandle = await projectDirectory.getFileHandle(fileName, { create: true });
    const writable = await fileHandle.createWritable();
    try {
      await writable.write(markdown);
      await writable.close();
    } catch (error) {
      await writable.abort?.();
      throw error;
    }

    const path = `ChatGPT/${sanitizeFileName(snapshot.projectName, "ChatGPT Project")}/${fileName}`;
    await mutateSettings((latest) => ({
      ...latest,
      conversationFiles: {
        ...(latest.conversationFiles ?? {}),
        [snapshot.conversationId]: {
          fileName,
          path,
          title: snapshot.title,
          contentHash: snapshot.contentHash,
          updatedAt
        }
      }
    }));

    let cleanupWarning = null;
    if (existing?.fileName && existing.fileName !== fileName) {
      try {
        await projectDirectory.removeEntry(existing.fileName);
      } catch (error) {
        if (error?.name !== "NotFoundError") {
          cleanupWarning = `The previous title file could not be removed: ${existing.fileName}`;
        }
      }
    }
    return {
      status: "written",
      path,
      conversationId: snapshot.conversationId,
      ...(cleanupWarning ? { cleanupWarning } : {})
    };
  } catch (cause) {
    if (cause?.code) throw cause;
    const error = new Error(`Markdown write failed: ${cause?.message ?? cause}`);
    error.code = ERROR_CODES.MARKDOWN_WRITE_ERROR;
    error.cause = cause;
    throw error;
  }
}

export function writeConversationSnapshot(rootHandle, snapshot) {
  const operation = filesystemTail.then(() => performConversationWrite(rootHandle, snapshot));
  filesystemTail = operation.catch(() => undefined);
  return operation;
}
