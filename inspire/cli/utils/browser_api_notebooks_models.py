"""Models for browser (web-session) notebook APIs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ImageInfo:
    """Docker image information."""

    image_id: str
    url: str
    name: str
    framework: str
    version: str


__all__ = ["ImageInfo"]
