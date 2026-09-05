import test from "node:test";
import assert from "node:assert/strict";
import {
  extractConversationId,
  extractProjectId,
  sanitizeFileName,
  validateProjectUrl
} from "../shared/utils.js";

test("sanitizeFileName replaces cross-platform invalid characters", () => {
  assert.equal(sanitizeFileName('a:b/c\\d?e*f"g<h>i|j'), "a-b-c-d-e-f-g-h-i-j");
  assert.equal(sanitizeFileName("CON"), "_CON");
  assert.equal(sanitizeFileName("title...   "), "title");
});

test("project and conversation IDs are extracted independently", () => {
  const url = "https://chatgpt.com/g/g-p-project-name/c/conversation-123";
  assert.equal(extractProjectId(url), "g-p-project-name");
  assert.equal(extractConversationId(url), "conversation-123");
  assert.equal(extractProjectId("https://example.com/g/g-p-wrong"), null);
});

test("project registration rejects a nested conversation URL", () => {
  assert.throws(
    () => validateProjectUrl("https://chatgpt.com/g/g-p-project/c/conversation-123"),
    /Project page URL/
  );
});
