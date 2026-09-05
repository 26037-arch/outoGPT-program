import { STORAGE_KEYS } from "../shared/constants.js";

const EMPTY_SETTINGS = Object.freeze({
  initialized: false,
  projectUrl: null,
  projectId: null,
  projectName: null,
  conversationFiles: {}
});

let mutationTail = Promise.resolve();

export async function getSettings() {
  const result = await chrome.storage.local.get(STORAGE_KEYS.SETTINGS);
  return { ...EMPTY_SETTINGS, ...(result[STORAGE_KEYS.SETTINGS] ?? {}) };
}

export async function setSettings(settings) {
  const value = { ...EMPTY_SETTINGS, ...settings };
  await chrome.storage.local.set({ [STORAGE_KEYS.SETTINGS]: value });
  return value;
}

export function mutateSettings(mutator) {
  const operation = mutationTail.then(async () => {
    const current = await getSettings();
    const next = await mutator(structuredClone(current));
    return setSettings(next);
  });
  mutationTail = operation.catch(() => undefined);
  return operation;
}

export async function setLastStatus(status) {
  await chrome.storage.local.set({
    [STORAGE_KEYS.LAST_STATUS]: { ...status, timestamp: new Date().toISOString() }
  });
}

export async function getLastStatus() {
  const result = await chrome.storage.local.get(STORAGE_KEYS.LAST_STATUS);
  return result[STORAGE_KEYS.LAST_STATUS] ?? null;
}

