"""Deterministic preparation of document images for model-facing analysis.

Prepared bytes are written below ``Documents/.prepared`` and are referenced
everywhere else by content-free handles.  This module never reads a prepared
cache entry: the model gateway is the sole reader and verifies its SHA-256
immediately before a provider call.
"""

from __future__ import annotations

from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Iterable

from PIL import Image, ImageOps, UnidentifiedImageError

from .workspaces import Workspace, WorkspaceError, write_json_atomic

PREPARATION_IMPLEMENTATION = "audit-workbench-document-media-v1"
PREPARATION_POLICY = {
    "implementation": PREPARATION_IMPLEMENTATION,
    "render_pdf_dpi": 160,
    "overview_long_edge": 2_048,
    "overview_max_pixels": 4_000_000,
    "tile_long_edge": 2_048,
    "tile_overlap": 64,
    "max_parts": 4,
    "max_pixels": 12_000_000,
    "max_bytes": 12 * 1024 * 1024,
}
MAX_VISUAL_PAGES = 20


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


PREPARATION_POLICY_HASH = _canonical_hash(PREPARATION_POLICY)


class MediaPreparationError(WorkspaceError):
    """A visual source could not be normalized within the declared policy."""

    def __init__(self, message: str, *, code: str = "visual_preparation_failed"):
        self.code = code
        super().__init__(f"{code}: {message}")


def planned_prepared_set_hash(
    source_sha1: str, page: int, *, frame: int | None = None
) -> str:
    """Identity available during read-only unit expansion."""

    return _canonical_hash(
        {
            "source_sha1": str(source_sha1),
            "page": int(page),
            "frame": int(frame or page),
            "policy_hash": PREPARATION_POLICY_HASH,
        }
    )


def _document(workspace: Workspace, document_id: str) -> dict:
    document = next(
        (
            item
            for item in workspace.documents
            if str(item.get("id")) == str(document_id)
        ),
        None,
    )
    if document is None:
        raise MediaPreparationError(f"Document '{document_id}' was not found.")
    return document


def _source_image(
    workspace: Workspace, document_id: str, page: int
) -> tuple[Image.Image, int]:
    from . import documents

    document = _document(workspace, document_id)
    path = documents.document_path(workspace, document)
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            try:
                import pypdfium2 as pdfium
            except ImportError as error:
                raise MediaPreparationError(
                    "PDF visual rendering requires pypdfium2."
                ) from error
            pdf = pdfium.PdfDocument(str(path))
            try:
                if not 1 <= int(page) <= len(pdf):
                    raise MediaPreparationError(
                        f"PDF page {page} does not exist."
                    )
                pdf_page = pdf[int(page) - 1]
                try:
                    width_points, height_points = pdf_page.get_size()
                    requested_scale = PREPARATION_POLICY["render_pdf_dpi"] / 72
                    safe_scale = min(
                        requested_scale,
                        8_192 / max(1.0, width_points, height_points),
                    )
                    rendered = pdf_page.render(scale=safe_scale)
                    try:
                        image = rendered.to_pil().copy()
                    finally:
                        rendered.close()
                finally:
                    pdf_page.close()
            finally:
                pdf.close()
            return image, int(page)

        with Image.open(path) as source:
            frame_count = int(getattr(source, "n_frames", 1) or 1)
            if not 1 <= int(page) <= frame_count:
                raise MediaPreparationError(
                    f"Image frame {page} does not exist."
                )
            source.seek(int(page) - 1)
            source.load()
            return source.copy(), int(page)
    except MediaPreparationError:
        raise
    except (
        OSError,
        UnidentifiedImageError,
        ValueError,
        Image.DecompressionBombError,
    ) as error:
        raise MediaPreparationError(
            "The source is not a supported or valid image."
        ) from error


def image_frame_count(path: Path) -> int:
    """Return a safely bounded deterministic frame count for an image source."""

    try:
        with Image.open(path) as image:
            return max(1, int(getattr(image, "n_frames", 1) or 1))
    except (
        OSError,
        UnidentifiedImageError,
        ValueError,
        Image.DecompressionBombError,
    ) as error:
        raise MediaPreparationError(
            "The source is not a supported or valid image."
        ) from error


def _normalized(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        return background.convert("RGB")
    return image.convert("RGB")


def _bounded(image: Image.Image, *, long_edge: int, max_pixels: int) -> Image.Image:
    width, height = image.size
    scale = min(
        1.0,
        long_edge / max(width, height),
        math.sqrt(max_pixels / max(1, width * height)),
    )
    if scale >= 1:
        return image.copy()
    size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _tile_boxes(width: int, height: int) -> list[tuple[int, int, int, int]]:
    long_edge = int(PREPARATION_POLICY["tile_long_edge"])
    overlap = int(PREPARATION_POLICY["tile_overlap"])
    stride = max(1, long_edge - overlap)
    xs = [0] if width <= long_edge else list(range(0, width, stride))
    ys = [0] if height <= long_edge else list(range(0, height, stride))
    if xs and xs[-1] + long_edge < width:
        xs.append(max(0, width - long_edge))
    if ys and ys[-1] + long_edge < height:
        ys.append(max(0, height - long_edge))
    boxes = []
    for y in ys:
        for x in xs:
            box = (x, y, min(width, x + long_edge), min(height, y + long_edge))
            if box not in boxes:
                boxes.append(box)
    return boxes


def _variants(image: Image.Image) -> list[tuple[str, tuple[int, int, int, int], Image.Image]]:
    width, height = image.size
    overview = _bounded(
        image,
        long_edge=int(PREPARATION_POLICY["overview_long_edge"]),
        max_pixels=int(PREPARATION_POLICY["overview_max_pixels"]),
    )
    values = [("overview", (0, 0, width, height), overview)]
    dense = width * height > int(PREPARATION_POLICY["overview_max_pixels"])
    unusual = max(width / max(1, height), height / max(1, width)) >= 3.0
    if dense or unusual:
        for box in _tile_boxes(width, height):
            tile = _bounded(
                image.crop(box),
                long_edge=int(PREPARATION_POLICY["tile_long_edge"]),
                max_pixels=int(PREPARATION_POLICY["overview_max_pixels"]),
            )
            values.append(("detail", box, tile))
            if len(values) >= int(PREPARATION_POLICY["max_parts"]):
                break
    return values


def _png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(
        output,
        format="PNG",
        optimize=False,
        compress_level=9,
        icc_profile=None,
        pnginfo=None,
    )
    return output.getvalue()


def _prepared_root(workspace: Workspace) -> Path:
    return workspace.root / "Documents" / ".prepared"


def _manifest_path(
    workspace: Workspace, source_sha1: str, page: int, frame: int
) -> Path:
    identity = planned_prepared_set_hash(source_sha1, page, frame=frame)
    return _prepared_root(workspace) / f"{identity.removeprefix('sha256:')}.json"


def _reusable_handles(
    workspace: Workspace,
    *,
    source_sha1: str,
    page: int,
    frame: int,
) -> list[dict] | None:
    """Load content-free preparation metadata without reading image bytes."""

    path = _manifest_path(workspace, source_sha1, page, frame)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        handles = payload["handles"]
        if (
            payload.get("source_sha1") != source_sha1
            or payload.get("policy_hash") != PREPARATION_POLICY_HASH
            or int(payload.get("page")) != page
            or int(payload.get("frame")) != frame
            or not isinstance(handles, list)
            or not handles
            or any(not isinstance(item, dict) for item in handles)
        ):
            raise ValueError("prepared-media manifest identity is invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise MediaPreparationError(
            "Prepared-media metadata is invalid."
        ) from error
    for handle in handles:
        cache_key = str(handle.get("cache_key") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", cache_key):
            raise MediaPreparationError(
                "Prepared-media metadata contains an invalid cache key."
            )
        if not (_prepared_root(workspace) / f"{cache_key}.png").is_file():
            raise MediaPreparationError(
                "Prepared media recorded for this source is missing."
            )
    return [dict(item) for item in handles]


def _set_hash(handles: Iterable[dict]) -> str:
    return _canonical_hash(
        [
            {
                "prepared_sha256": item["prepared_sha256"],
                "page": item["page"],
                "frame": item["frame"],
                "variant": item["variant"],
                "tile_order": item["tile_order"],
                "width": item["width"],
                "height": item["height"],
            }
            for item in handles
        ]
    )


def prepare_document_page(
    workspace: Workspace, document_id: str, page: int
) -> list[dict]:
    """Normalize one standalone image frame or rendered PDF page."""

    document = _document(workspace, document_id)
    source_sha1 = str(document.get("sha1") or "")
    if not source_sha1:
        raise MediaPreparationError("The source document has no content hash.")
    reusable = _reusable_handles(
        workspace,
        source_sha1=source_sha1,
        page=int(page),
        frame=int(page),
    )
    if reusable is not None:
        return reusable
    image, frame = _source_image(workspace, document_id, int(page))
    normalized = _normalized(image)
    handles: list[dict] = []
    total_pixels = total_bytes = 0
    pending: list[tuple[dict, bytes]] = []
    for order, (variant, bounds, part) in enumerate(_variants(normalized)):
        content = _png_bytes(part)
        width, height = part.size
        pixels = width * height
        if (
            total_pixels + pixels > int(PREPARATION_POLICY["max_pixels"])
            or total_bytes + len(content) > int(PREPARATION_POLICY["max_bytes"])
        ):
            continue
        prepared_sha = hashlib.sha256(content).hexdigest()
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "source_sha1": source_sha1,
                    "page": int(page),
                    "frame": frame,
                    "variant": variant,
                    "tile_order": order,
                    "bounds": list(bounds),
                    "prepared_sha256": prepared_sha,
                    "policy_hash": PREPARATION_POLICY_HASH,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        handle = {
            "cache_key": cache_key,
            "source_ref": (
                f"document:{document_id}:page:{int(page)}:"
                f"{variant}:{order}"
            ),
            "source_sha1": source_sha1,
            "prepared_sha256": f"sha256:{prepared_sha}",
            "page": int(page),
            "frame": frame,
            "variant": variant,
            "tile_order": order,
            "mime": "image/png",
            "width": width,
            "height": height,
            "prepared_byte_count": len(content),
            "pixel_count": pixels,
            "policy_hash": PREPARATION_POLICY_HASH,
            "prepared_set_hash": "",
        }
        pending.append((handle, content))
        total_pixels += pixels
        total_bytes += len(content)
    if not pending:
        raise MediaPreparationError(
            "The image could not fit within the prepared-media bounds."
        )
    prepared_set_hash = _set_hash(handle for handle, _content in pending)
    root = _prepared_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    for handle, content in pending:
        handle["prepared_set_hash"] = prepared_set_hash
        path = root / f"{handle['cache_key']}.png"
        if not path.exists():
            path.write_bytes(content)
        handles.append(handle)
    write_json_atomic(
        _manifest_path(workspace, source_sha1, int(page), frame),
        {
            "schema_version": 1,
            "source_sha1": source_sha1,
            "page": int(page),
            "frame": frame,
            "policy_hash": PREPARATION_POLICY_HASH,
            "prepared_set_hash": prepared_set_hash,
            "handles": handles,
        },
    )
    return handles


__all__ = [
    "MAX_VISUAL_PAGES",
    "MediaPreparationError",
    "PREPARATION_IMPLEMENTATION",
    "PREPARATION_POLICY",
    "PREPARATION_POLICY_HASH",
    "image_frame_count",
    "planned_prepared_set_hash",
    "prepare_document_page",
]
