"""Controller-specific errors with stable machine-readable codes."""


class ControllerError(Exception):
    code = "CONTROLLER_ERROR"


class UnknownChatError(ControllerError):
    code = "UNKNOWN_CHAT_ID"


class InvalidChatUrlError(ControllerError):
    code = "INVALID_CHAT_URL"


class BrowserNotOpenError(ControllerError):
    code = "BROWSER_NOT_OPEN"


class InvalidArgumentError(ControllerError):
    code = "INVALID_ARGUMENT"
