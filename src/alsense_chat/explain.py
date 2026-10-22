"""User-safe explanations ("Why this result?") built from what the backend actually did.

Rows use display names only: no internal ids, tenant ids, tool names or raw evidence.
"""

from .tools import TOOL_LABELS, Context

ASSISTANTS = {
    "scripted": "Scripted demo assistant · no AI model",
    "model": "AI model with backend-owned tools",
    "guided": "Guided check · decision tree, no AI model",
}


def humanize(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").strip().capitalize()


def lookups(ctx: Context) -> str:
    seen: list[str] = []
    for call in ctx.calls:
        label = TOOL_LABELS.get(call["tool"])
        if not label:
            continue
        result = call["result"]
        text = (
            f"{label} (failed: {humanize(result.removeprefix('error:')).lower()})"
            if result.startswith("error:")
            else label
        )
        if text not in seen:
            seen.append(text)
    return ", ".join(seen)


def explain(ctx: Context, rows: list[tuple[str, str]] | None = None, checks: str = "") -> list[dict]:
    items = [("Assistant", ASSISTANTS.get(ctx.assistant, ctx.assistant)), *(rows or [])]
    items.append(("Looked up", lookups(ctx)))
    documents = [doc["title"] for item in ctx.evidence if item["tool"] == "search_docs" for doc in item["data"]]
    items.append(("Evidence", ", ".join(documents)))
    items.append(("Reply checks", checks))
    items.append(("Data", "Synthetic demo data" if ctx.services.kind == "synthetic" else "Alsense APIs"))
    items.append(("Reference", ctx.correlation_id))
    return [{"label": label, "value": value} for label, value in items if value]
