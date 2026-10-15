from datetime import UTC, datetime
from uuid import uuid4

import pytest

from knowledge_service.domain.errors import ModelConfigurationLocked
from knowledge_service.domain.policies import ensure_model_binding_mutable, lock_model_binding


def test_locked_binding_rejects_model_change() -> None:
    current_embedding = uuid4()
    with pytest.raises(ModelConfigurationLocked):
        ensure_model_binding_mutable(
            models_locked_at=datetime.now(UTC),
            current_values={"embedding_model_id": current_embedding},
            requested_values={"embedding_model_id": uuid4()},
        )


def test_locked_binding_allows_unchanged_binding() -> None:
    embedding_id = uuid4()
    ensure_model_binding_mutable(
        models_locked_at=datetime.now(UTC),
        current_values={"embedding_model_id": embedding_id},
        requested_values={"embedding_model_id": embedding_id},
    )


def test_unlocked_binding_can_change() -> None:
    ensure_model_binding_mutable(
        models_locked_at=None,
        current_values={"rerank_model_id": None},
        requested_values={"rerank_model_id": uuid4()},
    )


def test_lock_model_binding_returns_timezone_aware_timestamp() -> None:
    assert lock_model_binding().tzinfo is not None
