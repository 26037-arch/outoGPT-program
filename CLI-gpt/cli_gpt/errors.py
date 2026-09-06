"""Domain exceptions exposed by CLI-GPT."""


class CliGptError(Exception):
    """Base class for expected CLI-GPT failures."""


class LoginRequired(CliGptError):
    pass


class InvalidProjectUrl(CliGptError):
    pass


class InvalidChatUrl(CliGptError):
    pass


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
    pass
