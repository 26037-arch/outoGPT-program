import test from "node:test";
import assert from "node:assert/strict";
import { CHAT_STATES, ERROR_CODES } from "../shared/constants.js";
import { ResponseStateMachine } from "../content/response-observer.js";

const options = {
  START_CONFIRM_COUNT: 2,
  START_TIMEOUT_MS: 100,
  END_CONFIRM_COUNT: 3,
  ASSISTANT_STABLE_MS: 50,
  RESPONSE_TIMEOUT_MS: 500
};

test("idle to confirmed generation to stable completion", () => {
  const machine = new ResponseStateMachine(options);
  machine.userMessageAdded(0);
  assert.equal(machine.poll(true, 10), CHAT_STATES.WAITING_FOR_START);
  assert.equal(machine.poll(true, 20), CHAT_STATES.GENERATING);
  machine.assistantChanged(25);
  assert.equal(machine.poll(false, 30), CHAT_STATES.WAITING_FOR_END);
  assert.equal(machine.poll(false, 40), CHAT_STATES.WAITING_FOR_END);
  assert.equal(machine.poll(false, 80), CHAT_STATES.COMPLETE);
});

test("single stop flicker does not confirm generation", () => {
  const machine = new ResponseStateMachine(options);
  machine.userMessageAdded(0);
  machine.poll(true, 10);
  assert.equal(machine.poll(false, 20), CHAT_STATES.WAITING_FOR_START);
  assert.equal(machine.stopPresentCount, 0);
});

test("stop returning while waiting for end resumes generating", () => {
  const machine = new ResponseStateMachine(options);
  machine.userMessageAdded(0);
  machine.poll(true, 10);
  machine.poll(true, 20);
  assert.equal(machine.poll(false, 30), CHAT_STATES.WAITING_FOR_END);
  assert.equal(machine.poll(true, 40), CHAT_STATES.GENERATING);
  assert.equal(machine.stopAbsentCount, 0);
});

test("generation start timeout is an error, never completion", () => {
  const machine = new ResponseStateMachine(options);
  machine.userMessageAdded(0);
  assert.equal(machine.poll(false, 101), CHAT_STATES.ERROR);
  assert.equal(machine.error, ERROR_CODES.START_TIMEOUT);
});

test("response timeout is an error", () => {
  const machine = new ResponseStateMachine(options);
  machine.userMessageAdded(0);
  machine.poll(true, 10);
  machine.poll(true, 20);
  assert.equal(machine.poll(true, 501), CHAT_STATES.ERROR);
  assert.equal(machine.error, ERROR_CODES.RESPONSE_TIMEOUT);
});

test("new user message after completion starts a new response cycle", () => {
  const machine = new ResponseStateMachine({ ...options, END_CONFIRM_COUNT: 1, ASSISTANT_STABLE_MS: 0 });
  machine.userMessageAdded(0);
  machine.poll(true, 1);
  machine.poll(true, 2);
  machine.assistantChanged(2);
  machine.poll(false, 3);
  machine.poll(false, 4);
  assert.equal(machine.state, CHAT_STATES.COMPLETE);
  assert.equal(machine.userMessageAdded(10), CHAT_STATES.WAITING_FOR_START);
  assert.equal(machine.assistantMutationObserved, false);
});

test("stop disappearance cannot complete without an assistant mutation", () => {
  const machine = new ResponseStateMachine({ ...options, END_CONFIRM_COUNT: 1, ASSISTANT_STABLE_MS: 0 });
  machine.userMessageAdded(0);
  machine.poll(true, 1);
  machine.poll(true, 2);
  machine.poll(false, 3);
  assert.equal(machine.poll(false, 4), CHAT_STATES.WAITING_FOR_END);
});

test("a stop signal arriving after the start deadline is a start timeout", () => {
  const machine = new ResponseStateMachine(options);
  machine.userMessageAdded(0);
  machine.poll(true, 10);
  assert.equal(machine.poll(true, 101), CHAT_STATES.ERROR);
  assert.equal(machine.error, ERROR_CODES.START_TIMEOUT);
});

test("completion observed after the response deadline is a response timeout", () => {
  const machine = new ResponseStateMachine({ ...options, END_CONFIRM_COUNT: 1, ASSISTANT_STABLE_MS: 0 });
  machine.userMessageAdded(0);
  machine.poll(true, 1);
  machine.poll(true, 2);
  machine.assistantChanged(3);
  assert.equal(machine.poll(false, 501), CHAT_STATES.ERROR);
  assert.equal(machine.error, ERROR_CODES.RESPONSE_TIMEOUT);
});
