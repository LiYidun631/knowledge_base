"""验证 Chroma 集合隔离、稳定 ID、分批写入和部分失败清理。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from knowledge_service.infrastructure.chroma import vector_store


class StubCollection:
    def __init__(self, metadata=None, *, fail=False):
        self.metadata = metadata or {}
        self.fail = fail
        self.deleted = []
        self.batches = []

    def delete(self, **kwargs):
        self.deleted.append(kwargs["where"])

    def upsert(self, **kwargs):
        self.batches.append(kwargs)
        if self.fail:
            raise OSError("private implementation detail")


class StubClient:
    def __init__(self, collection):
        self.collection = collection
        self.created = []

    def get_or_create_collection(self, **kwargs):
        self.created.append(kwargs)
        return self.collection


def _write(client, monkeypatch, *, vectors=None):
    profile = SimpleNamespace(id=uuid4(), dimensions=2)
    identifiers = [uuid4() for _ in range(3)]
    metadata = {
        "hnsw:space": "cosine",
        "schema_version": 1,
        "embedding_model_id": str(profile.id),
        "embedding_dimensions": 2,
    }
    client.collection.metadata = metadata
    monkeypatch.setattr(vector_store, "_client", lambda: client)
    monkeypatch.setattr(
        vector_store,
        "get_settings",
        lambda: SimpleNamespace(embedding_batch_size=2),
    )
    vector_store.upsert_chunks(
        embedding=profile,
        knowledge_base_id=identifiers[0],
        document_id=identifiers[1],
        document_version_id=identifiers[2],
        chunks=[
            {"id": uuid4(), "ordinal": index, "content": f"内容 {index}"} for index in range(3)
        ],
        vectors=vectors if vectors is not None else [[0.8, 0.2] for _ in range(3)],
    )
    return identifiers


def test_write_batches_and_scopes_metadata_to_kb_document_and_version(monkeypatch):
    collection = StubCollection()
    client = StubClient(collection)
    identifiers = _write(client, monkeypatch)

    assert collection.deleted == [{"document_version_id": str(identifiers[2])}]
    assert [len(batch["ids"]) for batch in collection.batches] == [2, 1]
    first = collection.batches[0]
    assert first["metadatas"][0]["knowledge_base_id"] == str(identifiers[0])
    assert first["metadatas"][0]["document_id"] == str(identifiers[1])
    assert first["metadatas"][0]["document_version_id"] == str(identifiers[2])


def test_partial_write_is_cleaned_and_reported_without_leaking_provider_error(monkeypatch):
    collection = StubCollection(fail=True)
    client = StubClient(collection)
    with pytest.raises(vector_store.ChromaWriteError, match="CHROMA_WRITE_ERROR") as error:
        _write(client, monkeypatch)

    assert "private implementation detail" not in str(error.value)
    assert len(collection.deleted) == 2


def test_collection_schema_mismatch_is_rejected_before_any_write(monkeypatch):
    collection = StubCollection(metadata={"embedding_dimensions": 768})
    client = StubClient(collection)
    monkeypatch.setattr(vector_store, "_client", lambda: client)

    with pytest.raises(vector_store.ChromaWriteError, match="SCHEMA_MISMATCH"):
        vector_store.upsert_chunks(
            embedding=SimpleNamespace(id=uuid4(), dimensions=2),
            knowledge_base_id=uuid4(),
            document_id=uuid4(),
            document_version_id=uuid4(),
            chunks=[{"id": uuid4(), "ordinal": 1, "content": "内容"}],
            vectors=[[0.8, 0.2]],
        )

    assert collection.deleted == []
    assert collection.batches == []


def test_delete_removes_every_document_version(monkeypatch):
    collection = StubCollection()
    profile = SimpleNamespace(id=uuid4(), dimensions=2)
    version_ids = [uuid4(), uuid4()]
    monkeypatch.setattr(vector_store, "_client", lambda: SimpleNamespace(
        get_collection=lambda name: collection
    ))
    monkeypatch.setattr(vector_store, "collection_name", lambda embedding: "test")

    vector_store.delete_document_versions(embedding=profile, version_ids=version_ids)

    assert collection.deleted == [
        {"document_version_id": str(version_id)} for version_id in version_ids
    ]


def test_delete_without_versions_does_not_open_chroma(monkeypatch):
    def unexpected_client():
        raise AssertionError("Chroma should not be opened for an empty version list")

    monkeypatch.setattr(vector_store, "_client", unexpected_client)

    vector_store.delete_document_versions(
        embedding=SimpleNamespace(id=uuid4(), dimensions=2), version_ids=[]
    )


def test_vector_dimension_mismatch_is_rejected(monkeypatch):
    collection = StubCollection()
    client = StubClient(collection)
    with pytest.raises(vector_store.ChromaWriteError, match="DIMENSION_MISMATCH"):
        _write(client, monkeypatch, vectors=[[0.8] for _ in range(3)])

    assert collection.batches == []
