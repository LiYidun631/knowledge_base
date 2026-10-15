from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from knowledge_service.domain.errors import ModelConfigurationLocked

MODEL_BINDING_FIELDS = {
    "embedding_model_id",
    "rerank_model_id",
}


def ensure_model_binding_mutable(
    *,
    models_locked_at: datetime | None,
    current_values: Mapping[str, Any],
    requested_values: Mapping[str, Any],
) -> None:
    if models_locked_at is None:
        return

    changed_fields = {
        field
        for field in MODEL_BINDING_FIELDS.intersection(requested_values)
        if requested_values[field] != current_values[field]
    }
    if changed_fields:
        raise ModelConfigurationLocked(
            f"Knowledge base model configuration is locked: {', '.join(sorted(changed_fields))}"
        )


def lock_model_binding(now: datetime | None = None) -> datetime:
    return now or datetime.now(UTC)
