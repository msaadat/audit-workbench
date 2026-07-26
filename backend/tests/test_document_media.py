from __future__ import annotations

from io import BytesIO
import json

from PIL import Image
import pytest

from app import document_media, documents, workspaces


def _image_bytes(size: tuple[int, int], *, file_format: str = "PNG") -> bytes:
    image = Image.new("RGB", size, "white")
    output = BytesIO()
    image.save(output, format=file_format)
    return output.getvalue()


def test_wide_image_preparation_is_bounded_ordered_and_content_free():
    workspace = workspaces.create_workspace("Wide visual source")
    document = documents.add_document(
        workspace,
        "org-chart.png",
        _image_bytes((6_000, 1_000)),
    )

    handles = document_media.prepare_document_page(
        workspace, document["id"], 1
    )

    assert [item["tile_order"] for item in handles] == list(range(len(handles)))
    assert handles[0]["variant"] == "overview"
    assert any(item["variant"] == "detail" for item in handles)
    assert len(handles) <= document_media.PREPARATION_POLICY["max_parts"]
    assert sum(item["pixel_count"] for item in handles) <= document_media.PREPARATION_POLICY["max_pixels"]
    assert sum(item["prepared_byte_count"] for item in handles) <= document_media.PREPARATION_POLICY["max_bytes"]
    assert len({item["prepared_set_hash"] for item in handles}) == 1
    serialized = json.dumps(handles)
    assert "base64" not in serialized
    assert "data:image" not in serialized
    assert str(workspace.root) not in serialized


def test_pdf_page_is_rendered_to_normalized_png():
    workspace = workspaces.create_workspace("Scanned PDF")
    document = documents.add_document(
        workspace,
        "scan.pdf",
        _image_bytes((800, 1_100), file_format="PDF"),
    )

    handles = document_media.prepare_document_page(
        workspace, document["id"], 1
    )

    assert handles
    assert handles[0]["mime"] == "image/png"
    assert handles[0]["page"] == 1
    assert handles[0]["prepared_sha256"].startswith("sha256:")


def test_missing_prepared_file_is_typed_failure_not_reprepared():
    workspace = workspaces.create_workspace("Missing prepared media")
    document = documents.add_document(
        workspace,
        "scan.png",
        _image_bytes((640, 480)),
    )
    handles = document_media.prepare_document_page(
        workspace, document["id"], 1
    )
    missing = (
        workspace.root
        / "Documents"
        / ".prepared"
        / f"{handles[0]['cache_key']}.png"
    )
    missing.unlink()

    with pytest.raises(
        document_media.MediaPreparationError,
        match="recorded for this source is missing",
    ):
        document_media.prepare_document_page(workspace, document["id"], 1)
    assert not missing.exists()
