"""原文件预览转换：TIFF、Office 转 PDF、缓存与缺少转换器的错误边界。"""

import asyncio
import io
from uuid import uuid4

import httpx
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.api.v1 import documents as document_api
from knowledge_service.application.documents import preview as preview_module
from knowledge_service.application.documents.parsing import DocumentParseError
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentVersion,
    KnowledgeBase,
    ModelProfile,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def _tiff_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 6), (220, 80, 60)).save(buffer, format="TIFF")
    return buffer.getvalue()


def test_tiff_is_converted_to_png_and_cached(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(preview_module, "_CACHE_DIR", tmp_path / "previews")
    version_id = uuid4()

    first = preview_module.build_preview(version_id, "扫描件.tiff", _tiff_bytes())
    second = preview_module.build_preview(version_id, "扫描件.tiff", b"ignored")

    assert first.media_type == "image/png"
    assert first.content.startswith(b"\x89PNG")
    assert second.content == first.content


def test_office_preview_uses_pdf_conversion_and_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(preview_module, "_CACHE_DIR", tmp_path / "previews")
    calls: list[str] = []

    def fake_convert(filename: str, content: bytes, target_extension: str) -> bytes:
        calls.append(target_extension)
        assert filename.endswith(".docx")
        return b"%PDF-1.4 preview"

    monkeypatch.setattr(preview_module, "convert_office_document", fake_convert)
    version_id = uuid4()

    first = preview_module.build_preview(version_id, "说明书.docx", b"docx")
    second = preview_module.build_preview(version_id, "说明书.docx", b"docx")

    assert first.media_type == "application/pdf"
    assert first.content == b"%PDF-1.4 preview"
    assert second.content == first.content
    assert calls == [".pdf"]


def test_missing_converter_is_reported(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(preview_module, "_CACHE_DIR", tmp_path / "previews")

    def missing(filename: str, content: bytes, target_extension: str) -> bytes:
        raise DocumentParseError("OFFICE_CONVERTER_NOT_CONFIGURED: 请安装 LibreOffice")

    monkeypatch.setattr(preview_module, "convert_office_document", missing)

    try:
        preview_module.build_preview(uuid4(), "旧版.doc", b"doc")
    except preview_module.PreviewConverterUnavailable as exc:
        assert "LibreOffice" in str(exc)
    else:
        raise AssertionError("缺少转换器时应抛出 PreviewConverterUnavailable")


def test_unsupported_preview_format() -> None:
    try:
        preview_module.build_preview(uuid4(), "说明.pdf", b"pdf")
    except preview_module.PreviewFormatUnsupported:
        pass
    else:
        raise AssertionError("PDF 不应进入服务端转换")


class _StubStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, content: bytes) -> None:
        self.objects[key] = content

    async def get(self, key: str) -> bytes:
        return self.objects[key]


def test_preview_endpoint_returns_503_without_converter(monkeypatch, tmp_path) -> None:
    settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
    storage = _StubStorage()
    monkeypatch.setattr(document_api, "get_object_storage", lambda settings: storage)
    monkeypatch.setattr(preview_module, "_CACHE_DIR", tmp_path / "previews")

    def missing(filename: str, content: bytes, target_extension: str) -> bytes:
        raise DocumentParseError("OFFICE_CONVERTER_NOT_CONFIGURED: 请安装 LibreOffice")

    monkeypatch.setattr(preview_module, "convert_office_document", missing)

    async def scenario() -> None:
        import main

        monkeypatch.setattr(main, "get_settings", lambda: settings)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'preview.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                profile = ModelProfile(
                    name="preview-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="test-embedding",
                    dimensions=2,
                )
                session.add(profile)
                await session.flush()
                knowledge_base = KnowledgeBase(name="预览知识库", embedding_model_id=profile.id)
                session.add(knowledge_base)
                await session.flush()
                document = Document(
                    knowledge_base_id=knowledge_base.id,
                    document_key="preview-1",
                    filename="旧版说明.doc",
                    latest_version_number=1,
                    status="completed",
                )
                session.add(document)
                await session.flush()
                version = DocumentVersion(
                    document_id=document.id,
                    version_number=1,
                    filename="旧版说明.doc",
                    media_type="application/msword",
                    size_bytes=3,
                    content_sha256="z" * 64,
                    object_key="k/doc.doc",
                    status="completed",
                    is_current=True,
                )
                session.add(version)
                await session.flush()
                await storage.put("k/doc.doc", b"doc")
                await session.commit()

                async def db_session():
                    yield session

                app = main.create_app()
                app.dependency_overrides[get_settings] = lambda: settings
                app.dependency_overrides[get_db_session] = db_session
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.get(
                        f"/api/v1/documents/{document.id}/preview",
                        headers={"Authorization": "Bearer admin-test-token"},
                    )
                assert response.status_code == 503
                assert "LibreOffice" in response.json()["detail"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())
