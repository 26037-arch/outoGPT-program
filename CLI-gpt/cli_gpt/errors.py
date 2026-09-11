"""Domain exceptions exposed by CLI-GPT."""


class CliGptError(Exception):
    """Base class for expected CLI-GPT failures."""


class LoginRequired(CliGptError):
    code = "LOGIN_REQUIRED"


class InvalidProjectUrl(CliGptError):
    code = "INVALID_PROJECT_URL"


class InvalidChatUrl(CliGptError):
    code = "INVALID_CHAT_URL"


class PromptBoxNotFound(CliGptError):
    pass


class PromptSendFailed(CliGptError):
    pass


class GenerationNotStarted(CliGptError):
    pass


class GenerationTimeout(CliGptError):
    pass


class BrowserError(CliGptError):
    """Base class for expected Chrome/CDP lifecycle failures."""


class BrowserLaunchFailed(BrowserError):
    """Backward-compatible name for a Chrome launch failure."""


class ChromeNotFound(BrowserLaunchFailed):
    pass


class ChromeLaunchFailed(BrowserLaunchFailed):
    pass


class ChromeDebugPortUnavailable(BrowserLaunchFailed):
    pass


class ChromeCdpConnectionFailed(BrowserError):
    pass


class ChromeProfileInUse(BrowserLaunchFailed):
    pass


class ChromeClosedUnexpectedly(BrowserError):
    pass


class NoChromeContext(BrowserError):
    pass


class NoChatGPTPage(BrowserError):
    pass


class LoginNotReady(BrowserError):
    pass


class InvalidExtensionPath(CliGptError):
    pass


class PageStructureChanged(CliGptError):
    code = "PAGE_STRUCTURE_CHANGED"


class ProjectAccessFailed(CliGptError):
    code = "PROJECT_ACCESS_FAILED"
