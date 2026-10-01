"""Pydantic schema for one triage decision. The agent cannot submit anything else."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

Line = Annotated[str, StringConstraints(max_length=21)]
Button = Annotated[str, StringConstraints(max_length=8)]


class Action(BaseModel):
    type: Literal[
        "none",
        "quarantine_client",
        "block_client",
        "unblock_client",
        "shelly_switch",
        "guest_voucher",
    ]
    params: dict = Field(default_factory=dict)


class Oled(BaseModel):
    lines: list[Line] = Field(min_length=1, max_length=4)
    button_a: Button
    button_b: Button

    @field_validator("lines")
    @classmethod
    def ascii_lines(cls, v):
        for line in v:
            if not line.isascii():
                raise ValueError("OLED lines must be ASCII")
        return v

    @field_validator("button_a", "button_b")
    @classmethod
    def ascii_buttons(cls, v):
        if not v.isascii():
            raise ValueError("OLED buttons must be ASCII")
        return v


class Triage(BaseModel):
    decision: Literal["ignore", "log", "notify", "propose", "greet"]
    severity: int = Field(ge=0, le=3)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=400)
    action: Action
    oled: Oled | None
    summary_line: str | None = Field(default=None, max_length=160)
    memory_note: str | None = Field(default=None, max_length=300)
    suspicious_input: bool
