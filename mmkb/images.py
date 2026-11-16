"""Image validation shared by resource imports and transient search uploads."""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import BinaryIO
import warnings

from PIL import Image, UnidentifiedImageError

from mmkb.errors import ResourceError

FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"),
           "WEBP": ("webp", "image/webp")}


def inspect_image(source: BinaryIO, filename: str | None = None, *, max_upload_bytes: int):
    """Inspect actual bytes, preserving them and rewinding the caller's stream."""
    source.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(64 * 1024):
        size += len(chunk)
        if size > max_upload_bytes:
            source.seek(0)
            raise ResourceError("file_too_large", "Image exceeds MAX_UPLOAD_BYTES", 413)
        digest.update(chunk)
    if size == 0:
        raise ResourceError("empty_file", "Image file is empty")
    source.seek(0)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                fmt = image.format
                if fmt not in FORMATS:
                    raise ResourceError("unsupported_format", "Only PNG, JPEG and WebP are supported", 415)
                width, height = image.size
                image.verify()
            source.seek(0)
            with Image.open(source) as image:
                image.load()
    except UnidentifiedImageError:
        raise ResourceError("unsupported_format", "File is not a supported image", 415) from None
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ResourceError("invalid_image", "Image is corrupt or exceeds safe image dimensions") from None
    finally:
        source.seek(0)
    extension, mime = FORMATS[fmt]
    extra = dict(size_bytes=size, mime_type=mime, width=width, height=height)
    if filename:
        extra["original_filename"] = PurePosixPath(filename.replace("\\", "/")).name
    return digest.hexdigest(), extension, extra
