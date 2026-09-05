import { findProjectNameCandidate } from "./selectors.js";
import { ERROR_CODES } from "../shared/constants.js";
import { extractProjectId, sleep } from "../shared/utils.js";

export async function detectProject({ timeoutMs = 20_000, stableCount = 3 } = {}) {
  const projectId = extractProjectId(location.href);
  if (!projectId) {
    return { ok: false, error: ERROR_CODES.PROJECT_NAME_EXTRACTION_ERROR, detail: "Current URL has no project ID." };
  }

  const deadline = Date.now() + timeoutMs;
  let previous = null;
  let confirmations = 0;
  while (Date.now() < deadline) {
    const candidate = findProjectNameCandidate(document);
    if (candidate?.name === previous) confirmations += 1;
    else {
      previous = candidate?.name ?? null;
      confirmations = candidate ? 1 : 0;
    }
    if (candidate && confirmations >= stableCount && document.readyState !== "loading") {
      return { ok: true, projectId, projectName: candidate.name, selector: candidate.selector, url: location.href };
    }
    await sleep(250);
  }
  return {
    ok: false,
    error: ERROR_CODES.PROJECT_NAME_EXTRACTION_ERROR,
    detail: "A stable project name could not be extracted from the ChatGPT DOM."
  };
}

