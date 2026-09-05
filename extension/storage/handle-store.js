import { ROOT_HANDLE_KEY } from "../shared/constants.js";

const DATABASE_NAME = "outogpt-file-handles";
const STORE_NAME = "handles";
const DATABASE_VERSION = 1;

function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE_NAME)) {
        request.result.createObjectStore(STORE_NAME);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function withStore(mode, action) {
  const database = await openDatabase();
  try {
    return await new Promise((resolve, reject) => {
      const transaction = database.transaction(STORE_NAME, mode);
      const request = action(transaction.objectStore(STORE_NAME));
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      transaction.onabort = () => reject(transaction.error);
    });
  } finally {
    database.close();
  }
}

export function saveRootHandle(handle) {
  return withStore("readwrite", (store) => store.put(handle, ROOT_HANDLE_KEY));
}

export function getRootHandle() {
  return withStore("readonly", (store) => store.get(ROOT_HANDLE_KEY));
}

export async function queryWritePermission(handle) {
  if (!handle) return "missing";
  return handle.queryPermission({ mode: "readwrite" });
}

export async function requestWritePermission(handle) {
  const current = await queryWritePermission(handle);
  if (current === "granted") return current;
  return handle.requestPermission({ mode: "readwrite" });
}

