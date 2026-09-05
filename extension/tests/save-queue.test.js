import test from "node:test";
import assert from "node:assert/strict";
import { LatestWriteQueue } from "../storage/filesystem.js";

test("latest pending snapshot wins while an older write is active", async () => {
  const queue = new LatestWriteQueue();
  const calls = [];
  let release;
  const gate = new Promise((resolve) => { release = resolve; });

  const first = queue.enqueue("conversation", async () => {
    calls.push("first");
    await gate;
    return "first-result";
  });
  const second = queue.enqueue("conversation", async () => { calls.push("second"); });
  const third = queue.enqueue("conversation", async () => {
    calls.push("third");
    return "third-result";
  });

  assert.deepEqual(await second, { status: "superseded" });
  release();
  assert.equal(await first, "first-result");
  assert.equal(await third, "third-result");
  assert.deepEqual(calls, ["first", "third"]);
});

