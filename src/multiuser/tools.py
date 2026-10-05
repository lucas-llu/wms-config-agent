"""Strict schemas for the authenticated, allowlisted tool surface only."""

from pydantic import BaseModel, ConfigDict, Field


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Start(Arguments):
    goal: str = Field(min_length=1, max_length=16000)
    workspace_id: str


class Get(Arguments):
    session_id: str = Field(min_length=1, max_length=128)


class Revision(Get):
    expected_revision: int = Field(ge=1, strict=True)


class Continue(Revision):
    message: str = Field(min_length=1, max_length=16000)


class Review(Revision):
    decision: str
    comment: str = Field(min_length=1, max_length=4000)


class Export(Revision):
    format: str


class Feedback(Get):
    revision: int = Field(ge=1, strict=True)
    kind: str
    reason: str = ""


class Summary(Get):
    revision: int = Field(ge=1, strict=True)


MODELS = {
    "start_configuration_session": Start,
    "get_configuration_session": Get,
    "continue_configuration_session": Continue,
    "validate_configuration_draft": Revision,
    "review_configuration_draft": Review,
    "export_configuration_solution": Export,
    "record_configuration_feedback": Feedback,
    "get_configuration_feedback_summary": Summary,
}


def schemas():
    return [
        {
            "name": name,
            "description": "Operate only on your authorized conversation.",
            "inputSchema": model.model_json_schema(),
        }
        for name, model in MODELS.items()
    ]


def parse(name, arguments):
    if name not in MODELS:
        raise ValueError("Unsupported authenticated tool")
    return MODELS[name].model_validate(arguments).model_dump()
