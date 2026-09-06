"""Domain exceptions exposed by CLI-GPT."""


class CliGptError(Exception):
    """Base class for expected CLI-GPT failures."""

    code = "CLI_GPT_ERROR"


class LoginRequired(CliGptError):
    code = "CHATGPT_LOGIN_REQUIRED"


class LoginVerificationFailed(CliGptError):
    code = "CHATGPT_LOGIN_VERIFICATION_FAILED"


class ChatGPTSessionRestoreFailed(CliGptError):
    code = "CHATGPT_SESSION_RESTORE_FAILED"


class InvalidProjectUrl(CliGptError):
    code = "INVALID_PROJECT_URL"


class InvalidChatUrl(CliGptError):
    code = "INVALID_CHAT_URL"


class PromptBoxNotFound(CliGptError):
    code = "PROMPT_BOX_NOT_FOUND"


class PromptSendFailed(CliGptError):
    code = "PROMPT_SEND_FAILED"


class GenerationNotStarted(CliGptError):
    code = "GENERATION_NOT_STARTED"


class GenerationTimeout(CliGptError):
    code = "GENERATION_TIMEOUT"


class BrowserLaunchFailed(CliGptError):
    code = "BROWSER_START_FAILED"


class PlaywrightNotInstalled(BrowserLaunchFailed):
    code = "PLAYWRIGHT_NOT_INSTALLED"


class PlaywrightChromiumNotInstalled(BrowserLaunchFailed):
    code = "PLAYWRIGHT_CHROMIUM_NOT_INSTALLED"


class ProfileInUse(BrowserLaunchFailed):
    code = "PROFILE_IN_USE"


class InvalidExtensionPath(CliGptError):
    code = "EXTENSION_NOT_FOUND"


class ExtensionLoadFailed(CliGptError):
    code = "EXTENSION_LOAD_FAILED"


class PageStructureChanged(CliGptError):
    code = "PAGE_STRUCTURE_CHANGED"


class ProjectAccessFailed(CliGptError):
    code = "PROJECT_ACCESS_FAILED"


class ConversationPageClosed(CliGptError):
    code = "CONVERSATION_PAGE_CLOSED"


class ControllerConnectionFailed(CliGptError):
    code = "CONTROLLER_CONNECTION_FAILED"
