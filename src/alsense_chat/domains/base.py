"""Pieces shared by every domain: argument schemas, handler records, refusals and approved links."""

from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from ..tools import Context


class InvalidRequest(ValueError):
    """A request the backend refuses, with a user-safe explanation that never echoes the input."""


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def no_inputs(args: dict) -> dict:
    return {}


@dataclass(frozen=True)
class Handler:
    """How the backend runs one declared tool."""

    schema: type[Arguments]
    run: Callable[["Context", dict], Any]
    summarize: Callable[[Any], str]
    label: str
    audit: Callable[[dict], dict] = field(default=no_inputs)


@dataclass(frozen=True)
class Choice:
    """A suggested answer for a clarification: the label to show and the message to send when it is picked."""

    label: str
    message: str


def safe_link(value: str) -> bool:
    parts = urlsplit(value)
    # Synthetic links are deliberately same-origin. Configure approved origins when a real adapter returns links.
    return (
        value.startswith("/reference/")
        and not value.startswith("//")
        and not parts.scheme
        and not parts.netloc
        and "\\" not in value
        and all(ord(char) >= 32 for char in value)
    )


def run_tool(ctx: "Context", name: str, args: dict) -> Generator[tuple[str, dict], None, dict]:
    """Run a tool for the scripted assistant, reporting progress the way the model path does."""
    yield "step", {"tool": name, "state": "started"}
    output = ctx.execute(name, args)
    yield "step", {"tool": name, "state": "failed" if "error" in output else "done"}
    return output
