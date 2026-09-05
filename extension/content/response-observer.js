import { CHAT_STATES, ERROR_CODES, TIMING } from "../shared/constants.js";

export class ResponseStateMachine {
  constructor(options = {}) {
    this.options = { ...TIMING, ...options };
    this.state = CHAT_STATES.IDLE;
    this.stopPresentCount = 0;
    this.stopAbsentCount = 0;
    this.waitStartedAt = null;
    this.lastAssistantMutationAt = 0;
    this.assistantMutationObserved = false;
    this.error = null;
  }

  userMessageAdded(now) {
    this.state = CHAT_STATES.WAITING_FOR_START;
    this.waitStartedAt = now;
    this.lastAssistantMutationAt = now;
    this.assistantMutationObserved = false;
    this.stopPresentCount = 0;
    this.stopAbsentCount = 0;
    this.error = null;
    return this.state;
  }

  assistantChanged(now) {
    if ([
      CHAT_STATES.WAITING_FOR_START,
      CHAT_STATES.GENERATING,
      CHAT_STATES.WAITING_FOR_END
    ].includes(this.state)) {
      this.lastAssistantMutationAt = now;
      this.assistantMutationObserved = true;
    }
  }

  poll(hasStop, now) {
    if (this.state === CHAT_STATES.WAITING_FOR_START) {
      if (now - this.waitStartedAt >= this.options.START_TIMEOUT_MS) {
        this.state = CHAT_STATES.ERROR;
        this.error = ERROR_CODES.START_TIMEOUT;
        return this.state;
      }
      this.stopPresentCount = hasStop ? this.stopPresentCount + 1 : 0;
      if (this.stopPresentCount >= this.options.START_CONFIRM_COUNT) {
        this.state = CHAT_STATES.GENERATING;
        this.stopAbsentCount = 0;
      }
    } else if (this.state === CHAT_STATES.GENERATING) {
      if (now - this.waitStartedAt >= this.options.RESPONSE_TIMEOUT_MS) {
        this.state = CHAT_STATES.ERROR;
        this.error = ERROR_CODES.RESPONSE_TIMEOUT;
        return this.state;
      }
      if (hasStop) {
        this.stopAbsentCount = 0;
      } else {
        this.stopAbsentCount = 1;
        this.state = CHAT_STATES.WAITING_FOR_END;
      }
    } else if (this.state === CHAT_STATES.WAITING_FOR_END) {
      if (now - this.waitStartedAt >= this.options.RESPONSE_TIMEOUT_MS) {
        this.state = CHAT_STATES.ERROR;
        this.error = ERROR_CODES.RESPONSE_TIMEOUT;
        return this.state;
      }
      if (hasStop) {
        this.stopAbsentCount = 0;
        this.state = CHAT_STATES.GENERATING;
      } else {
        this.stopAbsentCount += 1;
        const stopConfirmedAbsent = this.stopAbsentCount >= this.options.END_CONFIRM_COUNT;
        const assistantStable = this.assistantMutationObserved
          && now - this.lastAssistantMutationAt >= this.options.ASSISTANT_STABLE_MS;
        if (stopConfirmedAbsent && assistantStable) this.state = CHAT_STATES.COMPLETE;
      }
    }
    return this.state;
  }
}
