"""Controller adapter interfaces and default implementations."""

from .archive import ArchiveAdapter, PassiveExtensionArchiveAdapter
from .browser import BrowserAdapter

__all__ = ["ArchiveAdapter", "BrowserAdapter", "PassiveExtensionArchiveAdapter"]
