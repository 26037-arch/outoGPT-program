import { CHAT_STATES, DEBUG_PREFIX, ERROR_CODES, TIMING } from "../shared/constants.js";
import { makeAgentMessage, MESSAGE_TYPES } from "../shared/messages.js";
import { extractConversationId, extractProjectId } from "../shared/utils.js";
import { extractConversation } from "./conversation-extractor.js";
import { detectProject } from "./project-detector.js";
import { hasStopControl } from "./selectors.js";
import { ResponseStateMachine } from "./response-observer.js";

export class ChatAgent {
  constructor({ debug = true } = {}) {
    this.debug = debug;
    this.tabId = null;
    this.settings = null;
    this.url = location.href;
    this.conversationId = null;
    this.observer = null;
    this.pollTimer = null;
    this.urlTimer = null;
    this.streamSaveTimer = null;
    this.hydrationTimer = null;
    this.hydrating = false;
    this.machine = null;
    this.previousSnapshot = null;
    this.startedAnnounced = false;
  }

  log(message, ...extra) {
    if (this.debug) console.debug(`[${DEBUG_PREFIX}:ChatAgent:${this.tabId ?? "?"}] ${message}`, ...extra);
  }

  async start() {
    chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      this.handleMessage(message).then(sendResponse).catch((error) => sendResponse({ ok: false, error: error.message }));
      return true;
    });
    const registration = await chrome.runtime.sendMessage(makeAgentMessage(MESSAGE_TYPES.REGISTER_AGENT, {
      url: location.href
    }));
    this.tabId = registration?.tabId ?? null;
    this.settings = registration?.settings ?? null;
    await this.initializeForUrl();
    this.urlTimer = setInterval(() => {
      if (location.href !== this.url) this.initializeForUrl().catch((error) => this.reportError(error));
    }, TIMING.URL_POLL_INTERVAL_MS);
  }

  async handleMessage(message) {
    if (message?.type === MESSAGE_TYPES.DETECT_PROJECT) return detectProject();
    if (message?.type === MESSAGE_TYPES.GET_STATE) {
      return {
        ok: true,
        tabId: this.tabId,
        projectId: extractProjectId(location.href),
        conversationId: this.conversationId,
        state: this.machine?.state ?? CHAT_STATES.UNINITIALIZED
      };
    }
    if (message?.type === MESSAGE_TYPES.SAVE_MARKDOWN) {
      return this.requestSave("external_request");
    }
    if (message?.type === MESSAGE_TYPES.SEND_PROMPT) {
      return { ok: false, error: "SEND_PROMPT is reserved but is not implemented in this version." };
    }
    return undefined;
  }

  teardownConversation() {
    this.observer?.disconnect();
    this.observer = null;
    clearInterval(this.pollTimer);
    this.pollTimer = null;
    clearTimeout(this.streamSaveTimer);
    this.streamSaveTimer = null;
    clearTimeout(this.hydrationTimer);
    this.hydrationTimer = null;
    this.hydrating = false;
    this.machine = null;
    this.previousSnapshot = null;
    this.conversationId = null;
    this.startedAnnounced = false;
  }

  async initializeForUrl() {
    this.teardownConversation();
    this.url = location.href;
    const registration = await chrome.runtime.sendMessage(makeAgentMessage(MESSAGE_TYPES.REGISTER_AGENT, { url: this.url }));
    this.settings = registration?.settings ?? this.settings;
    const projectId = extractProjectId(this.url);
    const conversationId = extractConversationId(this.url);
    if (!this.settings?.initialized || projectId !== this.settings.projectId || !conversationId) {
      this.log("UNINITIALIZED (URL is not a registered project conversation)");
      return;
    }

    this.conversationId = conversationId;
    this.machine = new ResponseStateMachine();
    this.previousSnapshot = extractConversation({ projectName: this.settings.projectName });
    this.log(`initialized conversation=${conversationId}`);
    this.observer = new MutationObserver(() => this.handleDomMutation());
    this.observer.observe(document.body, { subtree: true, childList: true, characterData: true });
    this.pollTimer = setInterval(() => this.pollState(), TIMING.STATE_POLL_INTERVAL_MS);
    if (hasStopControl(document)) {
      this.machine.userMessageAdded(Date.now());
    } else {
      this.hydrating = true;
      this.scheduleHydrationCompletion();
    }
    await this.sendState();
    if (!this.hydrating && this.previousSnapshot?.messages.length) {
      await this.requestSave("conversation_opened");
    }
  }

  handleDomMutation() {
    if (!this.machine) return;
    if (location.href !== this.url) {
      this.initializeForUrl().catch((error) => this.reportError(error));
      return;
    }
    const snapshot = extractConversation({ projectName: this.settings.projectName });
    if (!snapshot) return;
    const previous = this.previousSnapshot;
    this.previousSnapshot = snapshot;
    if (!previous) return;

    if (this.hydrating) {
      if (hasStopControl(document)) {
        this.hydrating = false;
        clearTimeout(this.hydrationTimer);
        this.hydrationTimer = null;
        this.startedAnnounced = false;
        this.transition(() => this.machine.userMessageAdded(Date.now()));
        this.requestSave("generation_detected_during_load");
      } else if (snapshot.contentHash !== previous.contentHash) {
        this.scheduleHydrationCompletion();
      }
      return;
    }

    const previousUsers = previous.messages.filter((message) => message.role === "user").length;
    const currentUsers = snapshot.messages.filter((message) => message.role === "user").length;
    if (currentUsers > previousUsers) {
      this.startedAnnounced = false;
      this.transition(() => this.machine.userMessageAdded(Date.now()));
      this.requestSave("user_message_added");
    }

    const previousAssistant = [...previous.messages].reverse().find((message) => message.role === "assistant")?.markdown;
    const currentAssistant = [...snapshot.messages].reverse().find((message) => message.role === "assistant")?.markdown;
    if (currentAssistant !== previousAssistant) {
      this.machine.assistantChanged(Date.now());
      if ([
        CHAT_STATES.WAITING_FOR_START,
        CHAT_STATES.GENERATING,
        CHAT_STATES.WAITING_FOR_END
      ].includes(this.machine.state)) {
        clearTimeout(this.streamSaveTimer);
        this.streamSaveTimer = setTimeout(
          () => this.requestSave("stream_snapshot"),
          TIMING.STREAM_SAVE_DEBOUNCE_MS
        );
      }
    }

    if (snapshot.title !== previous.title && snapshot.contentHash !== previous.contentHash) {
      this.requestSave("title_changed");
    }
  }

  pollState() {
    if (!this.machine) return;
    if (location.href !== this.url) {
      this.initializeForUrl().catch((error) => this.reportError(error));
      return;
    }
    const stopPresent = hasStopControl(document);
    if (this.machine.state === CHAT_STATES.IDLE && stopPresent) {
      this.hydrating = false;
      clearTimeout(this.hydrationTimer);
      this.hydrationTimer = null;
      this.startedAnnounced = false;
      this.transition(() => this.machine.userMessageAdded(Date.now()));
      this.requestSave("generation_detected");
    }
    const before = this.machine.state;
    const previousPresentCount = this.machine.stopPresentCount;
    const previousAbsentCount = this.machine.stopAbsentCount;
    const after = this.machine.poll(stopPresent, Date.now());
    if (
      this.debug
      && (before !== after
        || previousPresentCount !== this.machine.stopPresentCount
        || previousAbsentCount !== this.machine.stopAbsentCount)
    ) {
      this.log(
        `${before} -> ${after}; stop=${stopPresent} `
        + `present=${this.machine.stopPresentCount}/${this.machine.options.START_CONFIRM_COUNT} `
        + `absent=${this.machine.stopAbsentCount}/${this.machine.options.END_CONFIRM_COUNT}`
      );
    }
    if (before !== after) {
      this.sendState();
      if (after === CHAT_STATES.GENERATING && !this.startedAnnounced) {
        this.startedAnnounced = true;
        chrome.runtime.sendMessage(makeAgentMessage(MESSAGE_TYPES.RESPONSE_STARTED, this.identity()));
      }
      if (after === CHAT_STATES.COMPLETE) {
        clearTimeout(this.streamSaveTimer);
        chrome.runtime.sendMessage(makeAgentMessage(MESSAGE_TYPES.RESPONSE_COMPLETED, this.identity()));
        this.requestSave("response_completed");
      }
      if (after === CHAT_STATES.ERROR) {
        this.reportError(Object.assign(new Error(this.machine.error), { code: this.machine.error }));
      }
    }
  }

  transition(action) {
    const before = this.machine.state;
    const after = action();
    if (before !== after) {
      this.log(`${before} -> ${after}`);
      this.sendState();
    }
  }

  scheduleHydrationCompletion() {
    clearTimeout(this.hydrationTimer);
    this.hydrationTimer = setTimeout(() => {
      if (!this.machine || !this.hydrating || location.href !== this.url) return;
      this.hydrating = false;
      this.hydrationTimer = null;
      this.previousSnapshot = extractConversation({ projectName: this.settings.projectName });
      this.log("initial conversation DOM stabilized");
      if (this.previousSnapshot?.messages.length) {
        this.requestSave("conversation_opened");
      }
    }, TIMING.ASSISTANT_STABLE_MS);
  }

  identity() {
    return {
      tabId: this.tabId,
      projectId: this.settings?.projectId,
      conversationId: this.conversationId,
      state: this.machine?.state ?? CHAT_STATES.UNINITIALIZED
    };
  }

  async sendState() {
    return chrome.runtime.sendMessage(makeAgentMessage(MESSAGE_TYPES.CHAT_STATE, this.identity()));
  }

  async requestSave(reason) {
    const snapshot = extractConversation({ projectName: this.settings?.projectName });
    if (!snapshot?.messages.length) return { ok: false, error: "No conversation messages were found." };
    this.previousSnapshot = snapshot;
    this.log(`save requested: ${reason}`);
    return chrome.runtime.sendMessage(makeAgentMessage(MESSAGE_TYPES.SAVE_CONVERSATION, {
      ...this.identity(),
      reason,
      snapshot
    }));
  }

  async reportError(error) {
    const code = error?.code || ERROR_CODES.DOM_SELECTOR_ERROR;
    console.error(`[${DEBUG_PREFIX}:ChatAgent:${this.tabId ?? "?"}] ${code}`, error);
    await chrome.runtime.sendMessage(makeAgentMessage(MESSAGE_TYPES.AGENT_ERROR, {
      ...this.identity(),
      state: CHAT_STATES.ERROR,
      error: code,
      detail: error?.message ?? String(error)
    }));
  }
}
