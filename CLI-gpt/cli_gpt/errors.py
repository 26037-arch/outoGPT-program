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


class BrowserLaunchFailed(CliGptError):
    pass


class InvalidExtensionPath(CliGptError):
    pass


class PageStructureChanged(CliGptError):
    pass
