"""Shared inbound Matrix media download and encrypted-file metadata handling."""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

IMAGE_EXT_BY_MIME: dict[str, str] = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
}


def encrypted_file_keys(encrypted_file: dict) -> Optional[tuple[str, str, str]]:
    """Return (key.k, hashes.sha256, iv), accepting a JWK dict or a bare key."""
    hashes, key = encrypted_file.get("hashes"), encrypted_file.get("key")
    digest = hashes.get("sha256") if isinstance(hashes, dict) else None
    key = key.get("k") if isinstance(key, dict) else key
    iv = encrypted_file.get("iv")
    if not all(isinstance(value, str) and value for value in (key, digest, iv)):
        return None
    return key, digest, iv


async def fetch_media_bytes(client: Any, url: str, event_id: str,
                            encrypted_file: Optional[dict]) -> Optional[bytes]:
    """Download and optionally decrypt media; transport and crypto errors propagate."""
    from .adapter import ContentURI

    data = await client.download_media(ContentURI(url))
    if data is None:
        return None
    if encrypted_file is not None:
        from mautrix.crypto.attachments import decrypt_attachment

        keys = encrypted_file_keys(encrypted_file)
        if keys is None:
            logger.warning("[Matrix] Encrypted media event missing decryption metadata for %s", event_id)
            return None
        data = decrypt_attachment(data, *keys)
    return data or None


def media_filename(content: dict) -> str:
    """Prefer the MSC2530 filename to the body, which may be a caption."""
    filename = content.get("filename")
    if isinstance(filename, str) and filename:
        return filename
    body = content.get("body")
    return body if isinstance(body, str) else ""
