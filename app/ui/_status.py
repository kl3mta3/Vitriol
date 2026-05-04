"""Shared Status enum — broken out so StatusGlyph and PlaylistItemWidget
can both import it without a circular dependency."""
from enum import Enum


class Status(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
