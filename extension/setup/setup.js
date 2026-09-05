import { MESSAGE_TYPES } from "../shared/messages.js";
import { STORAGE_KEYS } from "../shared/constants.js";
import { extractProjectId, sanitizeFileName, sleep, validateProjectUrl } from "../shared/utils.js";
import { ensureProjectDirectory } from "../storage/filesystem.js";
import { getRootHandle, queryWritePermission, requestWritePermission, saveRootHandle } from "../storage/handle-store.js";
import { getLastStatus, getSettings, setLastStatus, setSettings } from "../storage/settings.js";

const elements = {
  chooseFolder: document.querySelector("#choose-folder"),
  reauthorize: document.querySelector("#reauthorize"),
  folderStatus: document.querySelector("#folder-status"),
  projectUrl: document.querySelector("#project-url"),
  registerProject: document.querySelector("#register-project"),
  registration: document.querySelector("#registration"),
  projectName: document.querySelector("#project-name"),
  projectId: document.querySelector("#project-id"),
  projectPath: document.querySelector("#project-path"),
  status: document.querySelector("#status"),
  debugStatus: document.querySelector("#debug-status")
};

let rootHandle = null;

function showStatus(message, { error = false, detail = null } = {}) {
  elements.status.textContent = message;
  elements.status.classList.toggle("error", error);
  elements.debugStatus.textContent = detail ? JSON.stringify(detail, null, 2) : "";
}

async function updateFolderStatus() {
  rootHandle = await getRootHandle();
  if (!rootHandle) {
    elements.folderStatus.textContent = "선택된 폴더가 없습니다.";
    elements.reauthorize.hidden = true;
    return;
  }
  const permission = await queryWritePermission(rootHandle);
  elements.folderStatus.textContent = `${rootHandle.name} · 권한: ${permission}`;
  elements.reauthorize.hidden = permission === "granted";
}

async function chooseFolder() {
  if (!window.showDirectoryPicker) throw new Error("이 Chrome 버전은 File System Access API를 지원하지 않습니다.");
  const handle = await window.showDirectoryPicker({ mode: "readwrite", id: "outogpt-markdown-root" });
  const permission = await requestWritePermission(handle);
  if (permission !== "granted") throw new Error("선택한 폴더의 읽기/쓰기 권한이 허용되지 않았습니다.");
  await saveRootHandle(handle);
  rootHandle = handle;
  await updateFolderStatus();
  showStatus("루트 폴더가 저장되었습니다.");
}

async function waitForTabReady(tabId, timeoutMs = 30_000) {
  const initial = await chrome.tabs.get(tabId);
  if (initial.status === "complete") return;
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("ChatGPT 프로젝트 탭 로딩 시간이 초과되었습니다."));
    }, timeoutMs);
    function listener(updatedTabId, info) {
      if (updatedTabId === tabId && info.status === "complete") {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function findOrOpenProjectTab(projectUrl, projectId) {
  const tabs = await chrome.tabs.query({ url: "https://chatgpt.com/*" });
  const existing = tabs.find((tab) => extractProjectId(tab.url) === projectId);
  if (existing) {
    await chrome.tabs.update(existing.id, { active: true, url: projectUrl });
    return existing.id;
  }
  const created = await chrome.tabs.create({ url: projectUrl, active: true });
  return created.id;
}

async function detectProjectInTab(tabId) {
  let lastError = null;
  for (let attempt = 0; attempt < 12; attempt += 1) {
    try {
      const response = await chrome.tabs.sendMessage(tabId, { type: MESSAGE_TYPES.DETECT_PROJECT });
      if (response?.ok) return response;
      lastError = new Error(response?.detail || response?.error || "Project detection failed.");
    } catch (error) {
      lastError = error;
    }
    await sleep(500);
  }
  throw lastError || new Error("ChatGPT content script did not respond.");
}

async function registerProject() {
  const { projectUrl, projectId } = validateProjectUrl(elements.projectUrl.value);
  rootHandle = rootHandle || await getRootHandle();
  if (!rootHandle) throw new Error("먼저 Markdown을 저장할 루트 폴더를 선택하세요.");
  const permission = await requestWritePermission(rootHandle);
  if (permission !== "granted") throw new Error("폴더 권한을 다시 허용해야 합니다.");

  showStatus("ChatGPT 프로젝트 페이지를 열고 이름을 확인하는 중입니다...");
  const tabId = await findOrOpenProjectTab(projectUrl, projectId);
  await waitForTabReady(tabId);
  const detected = await detectProjectInTab(tabId);
  if (detected.projectId !== projectId) throw new Error("열린 페이지의 Project ID가 입력한 URL과 다릅니다.");

  await ensureProjectDirectory(rootHandle, detected.projectName);
  const previous = await getSettings();
  const settings = await setSettings({
    initialized: true,
    projectUrl,
    projectId,
    projectName: detected.projectName,
    conversationFiles: previous.projectId === projectId ? previous.conversationFiles : {}
  });
  await setLastStatus({ type: "PROJECT_REGISTERED", projectId, projectName: detected.projectName });
  renderRegistration(settings);
  showStatus("프로젝트가 등록되었습니다.", { detail: { selector: detected.selector, tabId } });
}

function renderRegistration(settings) {
  if (!settings.initialized) {
    elements.registration.hidden = true;
    return;
  }
  elements.registration.hidden = false;
  elements.projectName.textContent = settings.projectName;
  elements.projectId.textContent = settings.projectId;
  elements.projectPath.textContent = `ChatGPT/${sanitizeFileName(settings.projectName)}/`;
  elements.projectUrl.value = settings.projectUrl;
}

async function restore() {
  const [settings, lastStatus] = await Promise.all([getSettings(), getLastStatus()]);
  renderRegistration(settings);
  await updateFolderStatus();
  if (lastStatus) showStatus(lastStatus.error ? `오류: ${lastStatus.error}` : (lastStatus.type || lastStatus.status || "준비됨"), {
    error: Boolean(lastStatus.error),
    detail: lastStatus
  });
}

async function runWithButton(button, action) {
  button.disabled = true;
  try {
    await action();
  } catch (error) {
    if (error?.name !== "AbortError") showStatus(error.message || String(error), { error: true });
  } finally {
    button.disabled = false;
  }
}

elements.chooseFolder.addEventListener("click", () => runWithButton(elements.chooseFolder, chooseFolder));
elements.reauthorize.addEventListener("click", () => runWithButton(elements.reauthorize, async () => {
  const permission = await requestWritePermission(rootHandle);
  await updateFolderStatus();
  showStatus(permission === "granted" ? "폴더 권한이 복원되었습니다." : "폴더 권한이 거부되었습니다.", { error: permission !== "granted" });
}));
elements.registerProject.addEventListener("click", () => runWithButton(elements.registerProject, registerProject));

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes[STORAGE_KEYS.LAST_STATUS]) return;
  const status = changes[STORAGE_KEYS.LAST_STATUS].newValue;
  showStatus(status?.error ? `오류: ${status.error}` : (status?.type || status?.status || "상태 갱신"), {
    error: Boolean(status?.error),
    detail: status
  });
});

restore().catch((error) => showStatus(error.message || String(error), { error: true }));

