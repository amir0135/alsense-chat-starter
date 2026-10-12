import json
import logging
import re
from collections.abc import Iterator
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings
from .tools import Context, evidence_text, model_definition, tool_definitions

log = logging.getLogger(__name__)


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["answer", "clarification", "not_in_data", "out_of_scope"]
    text: str = Field(min_length=1, max_length=6000)
    source_ids: list[str] = Field(max_length=20)


def guard(raw: str, ctx: Context) -> Reply:
    reply = Reply.model_validate_json(raw)
    known = {item["source_id"] for item in ctx.evidence}
    if not set(reply.source_ids) <= known:
        raise ValueError("Unknown evidence reference.")
    if reply.status == "answer" and (not reply.source_ids or not ctx.evidence):
        raise ValueError("Answer requires tool evidence.")
    if reply.status == "clarification" and "?" not in reply.text:
        raise ValueError("Clarification requires a question.")
    if re.search(r"https?://|www\.", reply.text, re.IGNORECASE):
        raise ValueError("Links must come from approved structured tool results.")
    numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", evidence_text(ctx)))
    if not set(re.findall(r"\b\d+(?:[.,]\d+)?\b", reply.text)) <= numbers:
        raise ValueError("Number absent from tool evidence.")
    if ctx.actions and re.search(r"\b(submitted|sent|created successfully)\b", reply.text, re.IGNORECASE):
        raise ValueError("A draft has not been submitted.")
    return reply


def envelope(reply: Reply, ctx: Context) -> dict:
    if reply.status in {"not_in_data", "out_of_scope"}:
        ctx.store.record_unanswered(ctx.session_id, reply.status)
    return {**reply.model_dump(), "sources": ctx.evidence, "actions": ctx.actions, "synthetic": True}


def demo_run(message: str, ctx: Context) -> Iterator[tuple[str, dict]]:
    """Deterministic local harness, not an AI model. Slash commands exercise real tool handlers."""
    commands = {
        "/sites": "resolve_sites", "/reports": "list_report_types", "/docs": "search_docs",
        "/report": "prepare_report", "/ticket": "prepare_support_ticket", "/status": "get_report_status",
    }
    command, _, rest = message.partition(" ")
    name = commands.get(command.lower())
    if name is None:
        normalized = message.casefold()
        if ("site" in normalized and any(word in normalized for word in
                                         ("which", "access", "available", "list", "my sites"))):
            name, args = "resolve_sites", {"query": "*"}
        elif ("report" in normalized and any(word in normalized for word in
                                             ("available", "types", "options", "list"))):
            name, args = "list_report_types", {}
        elif any(word in normalized for word in ("alarm", "report", "sensor", "offline")):
            name, args = "search_docs", {"query": message}
        else:
            reply = Reply(status="out_of_scope", text="This demo covers operations documentation, sites, "
                          "reports and support drafts. Use the buttons or slash commands.", source_ids=[])
            yield "reply", envelope(reply, ctx)
            return
    elif name == "list_report_types":
        args = {}
    elif name in {"prepare_report", "prepare_support_ticket"}:
        try:
            args = json.loads(rest)
        except json.JSONDecodeError:
            yield "reply", envelope(Reply(status="clarification", text="Can you supply all form fields "
                                         "using the Report or Support button?", source_ids=[]), ctx)
            return
    elif name == "get_report_status":
        args = {"action_id": rest.strip()}
    else:
        args = {"query": rest.strip() or ("*" if name == "resolve_sites" else "")}
    yield "step", {"tool": name, "state": "started"}
    output = ctx.execute(name, args)
    yield "step", {"tool": name, "state": "failed" if "error" in output else "done"}
    if "error" in output:
        reply = Reply(status="clarification", text="Can you check the required fields and choose a site "
                      "from your permitted list?", source_ids=[])
    elif not output["data"]:
        reply = Reply(status="not_in_data", text="The permitted data does not answer that lookup. "
                      "You can prepare a support ticket.", source_ids=[])
    else:
        text = "See the returned evidence below."
        if name.startswith("prepare_"):
            text = "Draft prepared only. Review the form and press Submit to send it to the synthetic API."
        if name == "search_docs":
            text = " ".join(doc["excerpt"] for doc in output["data"])
        reply = Reply(status="answer", text=text, source_ids=[output["source_id"]])
    yield "reply", envelope(guard(reply.model_dump_json(), ctx), ctx)


class FoundryRunner:
    def __init__(self, settings: Settings):
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential

        self.credential = DefaultAzureCredential()
        self.project = AIProjectClient(endpoint=settings.foundry_endpoint, credential=self.credential)
        self.client = self.project.get_openai_client()
        self.deployment = settings.deployment

    def close(self) -> None:
        self.client.close()
        self.project.close()
        self.credential.close()

    def run(self, message: str, ctx: Context) -> Iterator[tuple[str, dict]]:
        history = ctx.store.history(ctx.session_id)
        items = []
        for turn in history:
            items.extend([{"role": "user", "content": turn["question"]},
                          {"role": "assistant", "content": json.dumps({
                              key: turn["reply"][key] for key in ("status", "text", "source_ids")
                          })}])
        items.append({"role": "user", "content": message})
        instructions = model_definition()["instructions"]
        repaired = False
        for _ in range(12):
            response = self.client.responses.create(
                model=self.deployment, instructions=instructions, input=items,
                tools=tool_definitions(), store=False, max_output_tokens=3000,
            )
            if response.status != "completed":
                raise RuntimeError("Model response incomplete or blocked; no answer released.")
            calls = [item for item in response.output if item.type == "function_call"]
            if calls:
                items.extend(item.model_dump(exclude_none=True) for item in response.output)
                for call in calls:
                    yield "step", {"tool": call.name, "state": "started"}
                    try:
                        args = json.loads(call.arguments)
                    except json.JSONDecodeError:
                        output = {"error": "Invalid JSON arguments."}
                    else:
                        output = ctx.execute(call.name, args)
                    yield "step", {"tool": call.name, "state": "failed" if "error" in output else "done"}
                    items.append({"type": "function_call_output", "call_id": call.call_id,
                                  "output": json.dumps(output)})
                continue
            try:
                reply = guard(response.output_text, ctx)
            except (ValidationError, ValueError):
                log.warning("Final reply failed structural or evidence checks")
                if not repaired:
                    repaired = True
                    items.extend(item.model_dump(exclude_none=True) for item in response.output)
                    items.append({"role": "user", "content": "Your reply failed validation. Return the required "
                                  "JSON, reference this turn's evidence, use no unsupported numbers or links, "
                                  "and never claim a draft was sent. Otherwise return not_in_data."})
                    continue
                reply = Reply(status="not_in_data", text="I could not verify an answer from the tool evidence. "
                              "You can prepare a support ticket.", source_ids=[])
            yield "reply", envelope(reply, ctx)
            return
        raise RuntimeError("Tool round limit reached; ask a narrower question.")
