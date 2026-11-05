"""Local image preflight and Matrix attachment uploads; never fetch remote URLs."""

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Any

from .protocol import InteractiveUiError


@dataclass
class ImageUpload:
    image: dict[str, Any]
    path: str
    data: bytes
    mimetype: str


def _image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read_image(value: object, limit: int, path: str) -> tuple[bytes, str] | InteractiveUiError:
    if not isinstance(value, str) or not Path(value).is_absolute():
        return InteractiveUiError(path, "bad_value", "upload must be an absolute local regular image file")
    try:
        # Nonblocking open prevents a FIFO masquerading as an image from stalling the gateway.
        fd = os.open(value, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                return InteractiveUiError(path, "bad_value", f"upload must be a regular image of at most {limit} bytes")
            data = stream.read(limit + 1)
    except (OSError, ValueError):
        return InteractiveUiError(path, "bad_value", "upload must be a readable local regular image file")
    mimetype = _image_type(data)
    if not mimetype or len(data) > limit:
        return InteractiveUiError(path, "bad_value", f"upload must have PNG, JPEG, GIF or WebP magic and at most {limit} bytes")
    return data, mimetype


def _images(payload):
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        if node.get("type") == "image":
            yield node, f"nodes[{i}]"
        if node.get("type") == "carousel" and isinstance(node.get("items"), list):
            for j, item in enumerate(node["items"]):
                if isinstance(item, dict) and isinstance(item.get("image"), dict):
                    yield item["image"], f"nodes[{i}].items[{j}].image"


def prepare_uploads(payload: dict, *, max_bytes: int) -> list[ImageUpload] | InteractiveUiError:
    """Read all files before uploads; placeholders allow full protocol preflight."""
    uploads = []
    for image, path in _images(payload):
        if "upload" not in image:
            continue
        if "url" in image or "file" in image:
            return InteractiveUiError(path + ".upload", "bad_value", "Use upload instead of url or file")
        result = _read_image(image.pop("upload"), max_bytes, path + ".upload")
        if isinstance(result, InteractiveUiError):
            return result
        data, mimetype = result
        image.update(url="mxc://upload.invalid/pending", mimetype=mimetype)
        uploads.append(ImageUpload(image, path, data, mimetype))
    return uploads


async def _needs_encrypted_upload(adapter, room_id: str) -> bool:
    """Only an explicit unencrypted state permits plaintext UI media uploads."""
    from mautrix.errors import MNotFound
    from mautrix.types import EventType, RoomID

    # Unencrypted rooms have no encryption event, so sync may leave their cache empty.
    state = await adapter._client.state_store.is_encrypted(RoomID(room_id))
    if state is None:
        try:
            await adapter._client.get_state_event(RoomID(room_id), EventType.ROOM_ENCRYPTION)
        except MNotFound:
            state = False
        else:
            state = True
    if state is not True and state is not False:
        raise ValueError("Room encryption state unavailable")
    crypto_available = adapter._encryption and getattr(adapter._client, "crypto", None) is not None
    if (state or adapter._e2ee_mode == "required") and not crypto_available:
        raise ValueError("Room encryption unavailable")
    return state


async def prepare_wire_uploads(adapter, room_id: str, uploads: list[ImageUpload]) -> None:
    """Prepare encryption metadata so its full size is checked before publishing files."""
    if not uploads or not await _needs_encrypted_upload(adapter, room_id):
        return
    from mautrix.crypto.attachments import encrypt_attachment

    for upload in uploads:
        upload.data, encrypted_file = encrypt_attachment(upload.data)
        upload.image["file"] = {**encrypted_file.serialize(), "url": upload.image.pop("url")}


async def resolve_uploads(adapter, uploads: list[ImageUpload]) -> None:
    """Replace prevalidated image sources with homeserver upload results."""
    for upload in uploads:
        # A neutral filename avoids disclosing local paths or model-supplied filenames.
        url = str(await adapter._client.upload_media(
            upload.data, mime_type=upload.mimetype, filename="image", size=len(upload.data)))
        if "file" in upload.image:
            upload.image["file"]["url"] = url
        else:
            upload.image["url"] = url
