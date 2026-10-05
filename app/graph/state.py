"""Validate saved channel values before selecting or executing a graph node."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from app.graph.serde import CheckpointError


class SavedTurn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    graph_version: str
    dataset_id: str
    metric_version: str
    policy_version: str
    question: str
    effective_question: str
    chosen: dict[str, str]
    asking: dict[str, JsonValue] | None
    named_accounts: list[list[str]]
    plan: dict[str, JsonValue] | None
    planning: dict[str, JsonValue] | None
    disclosures: list[str]
    model_calls: int = Field(ge=0)
    route: Literal["ask", "wait", "resolve", "plan", "check", "answer", "end"] | None = None
    outcome: str | None = None


def validate_state(state):
    try:
        SavedTurn.model_validate(state)
    except ValidationError:
        raise CheckpointError("Saved turn state is invalid") from None
