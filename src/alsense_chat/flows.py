"""Guided troubleshooting: deterministic decision trees that operations teams own and the backend runs.

Flows are YAML data. Each request replays a flow from its start with the user's answers so far, so no
flow state is stored and every lookup is re-checked with the current session's permissions.
"""

import difflib
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

from .config import ROOT
from .tools import Context

SAMPLE_FLOWS = ROOT / "troubleshooting.yaml"
NODE_KINDS = ("ask", "identify_site", "check", "outcome")
BRANCHES = {"ask": ("yes", "no"), "identify_site": ("found", "not_found"), "check": ("yes", "no"), "outcome": ()}
CHECK_TOOLS = {"site_health": "check_site_health"}
YES_NO = [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]
DEFAULT_RETRY = "I couldn't find that site. Which company and site is it?"
DEFAULT_CONFIRM = "Do you mean site {site}?"
MAX_ANSWERS = 40
MAX_ANSWER_LENGTH = 200
PENDING = object()


class FlowError(ValueError):
    pass


def load_flows(path: str = "") -> dict[str, dict]:
    data = yaml.safe_load(Path(path or SAMPLE_FLOWS).read_text(encoding="utf-8")) or {}
    flows = data.get("flows") if isinstance(data, dict) else None
    if not isinstance(flows, dict) or not flows:
        raise FlowError("A flows file needs a non-empty 'flows' mapping.")
    for flow_id, flow in flows.items():
        validate_flow(flow_id, flow)
    return flows


def validate_flow(flow_id: str, flow: dict) -> None:
    if not isinstance(flow_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", flow_id):
        raise FlowError(f"Flow id {flow_id!r} must use lowercase letters, digits and hyphens.")
    if not isinstance(flow, dict) or not all(flow.get(key) for key in ("title", "start", "nodes")):
        raise FlowError(f"{flow_id}: title, start and nodes are required.")
    nodes = flow["nodes"]
    if not isinstance(nodes, dict) or flow["start"] not in nodes:
        raise FlowError(f"{flow_id}: start must name a node.")
    for trigger in flow.get("triggers", []):
        if not isinstance(trigger, list) or not trigger or not all(isinstance(t, str) and t.strip() for t in trigger):
            raise FlowError(f"{flow_id}: each trigger is a non-empty list of words.")
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            raise FlowError(f"{flow_id}.{node_id}: a node is a mapping.")
        # YAML 1.1 reads bare yes/no keys as booleans; authors should not have to quote them.
        for flag, name in ((True, "yes"), (False, "no")):
            if flag in node:
                node[name] = node.pop(flag)
        kinds = [kind for kind in NODE_KINDS if kind in node]
        if len(kinds) != 1:
            raise FlowError(f"{flow_id}.{node_id}: use exactly one of {', '.join(NODE_KINDS)}.")
        kind = kinds[0]
        if not isinstance(node[kind], str) or not node[kind].strip():
            raise FlowError(f"{flow_id}.{node_id}: {kind} needs text.")
        for branch in BRANCHES[kind]:
            if node.get(branch) not in nodes:
                raise FlowError(f"{flow_id}.{node_id}: '{branch}' must name a node.")
        if kind == "check" and (node["check"] not in CHECK_TOOLS or not node.get("any_open")):
            raise FlowError(f"{flow_id}.{node_id}: check must be one of {sorted(CHECK_TOOLS)} with any_open types.")
        if kind == "outcome" and node.get("kind", "resolve") not in {"resolve", "ticket"}:
            raise FlowError(f"{flow_id}.{node_id}: outcome kind must be resolve or ticket.")
        if kind == "identify_site" and "{site}" not in node.get("confirm", DEFAULT_CONFIRM):
            raise FlowError(f"{flow_id}.{node_id}: confirm text must contain {{site}}.")
    _reject_loops(flow_id, nodes, flow["start"])


def _reject_loops(flow_id: str, nodes: dict, start: str) -> None:
    """Flows must be acyclic so that replaying the answers always ends."""
    finished: set[str] = set()
    path: list[str] = []

    def visit(node_id: str) -> None:
        if node_id in path:
            raise FlowError(f"{flow_id}: the flow loops back to {node_id}.")
        if node_id in finished:
            return
        path.append(node_id)
        node = nodes[node_id]
        kind = next(kind for kind in NODE_KINDS if kind in node)
        for branch in BRANCHES[kind]:
            visit(node[branch])
        path.pop()
        finished.add(node_id)

    visit(start)


def match_trigger(flows: dict, message: str) -> str | None:
    text = message.casefold()
    for flow_id, flow in flows.items():
        if any(all(term.casefold() in text for term in trigger) for trigger in flow.get("triggers", [])):
            return flow_id
    return None


def _reply(status: str, text: str, ctx: Context, flow: dict, source_ids: list[str] | None = None) -> dict:
    # Flow text is authored by the content owner, not generated, so it skips the model reply guard.
    return {"status": status, "text": text, "source_ids": list(source_ids or []), "sources": ctx.evidence,
            "actions": [], "synthetic": True, "flow": flow}


def menu_reply(ctx: Context) -> dict:
    if not ctx.flows:
        return _reply("out_of_scope", "Guided checks aren't configured in this demo.", ctx,
                      {"kind": "menu", "options": []})
    options = [{"value": flow_id, "label": flow["title"], "description": flow.get("description", "")}
               for flow_id, flow in ctx.flows.items()]
    return _reply("clarification", "What do you need help with?", ctx, {"kind": "menu", "options": options})


def run_flow(flow_id: str, answers: list, ctx: Context) -> Iterator[tuple[str, dict]]:
    flow = ctx.flows.get(flow_id)
    if flow is None:
        raise FlowError("Unknown guided check.")
    if (not isinstance(answers, list) or len(answers) > MAX_ANSWERS
            or not all(isinstance(answer, str) and len(answer) <= MAX_ANSWER_LENGTH for answer in answers)):
        raise FlowError("Invalid answers.")
    yield from _Walk(flow_id, flow, answers, ctx).run()


def _yes_no(answer: str) -> str | None:
    return {"yes": "yes", "no": "no"}.get(answer.strip().casefold())


def _exact_site(text: str, sites: list[dict]) -> dict | None:
    folded = " ".join(text.casefold().split())
    matches = [site for site in sites if site["name"].casefold() in folded]
    return max(matches, key=lambda site: len(site["name"])) if matches else None


def _closest_site(text: str, sites: list[dict]) -> dict | None:
    folded = " ".join(text.casefold().split())
    if len(folded) < 3:
        return None
    best, best_score = None, 0.0
    for site in sites:
        name = site["name"].casefold()
        score = difflib.SequenceMatcher(None, folded, name).ratio()
        if folded in name:
            score = max(score, 0.75)
        for word in folded.split():
            if len(word) >= 4 and any(difflib.SequenceMatcher(None, word, part).ratio() >= 0.8
                                      for part in name.split()):
                score = max(score, 0.65)
        if score > best_score:
            best, best_score = site, score
    return best if best_score >= 0.6 else None


class _Walk:
    def __init__(self, flow_id: str, flow: dict, answers: list[str], ctx: Context):
        self.flow_id, self.flow, self.ctx = flow_id, flow, ctx
        self.answers = answers
        self.used = 0
        self.trail: list[dict] = []
        self.site: dict | None = None
        self.source_ids: list[str] = []

    def next_answer(self) -> str | None:
        if self.used >= len(self.answers):
            return None
        self.used += 1
        return self.answers[self.used - 1].strip()

    def frame(self, kind: str, **extra) -> dict:
        return {"id": self.flow_id, "title": self.flow["title"], "kind": kind, "step": self.used + 1,
                "answers": self.answers[: self.used], **extra}

    def prompt(self, text: str, kind: str, options: list[dict], free_text: bool = False) -> dict:
        return _reply("clarification", text, self.ctx, self.frame(kind, options=options, free_text=free_text),
                      self.source_ids)

    def single_site(self) -> dict | None:
        sites = [site for site in self.ctx.user.sites if site.tenant_id == self.ctx.user.tenant_id]
        return {"id": sites[0].id, "name": sites[0].name} if len(sites) == 1 else None

    def run(self) -> Iterator[tuple[str, dict]]:
        nodes = self.flow["nodes"]
        node_id = self.flow["start"]
        for _ in range(len(nodes) + 1):
            node = nodes[node_id]
            if "ask" in node:
                answer = self.next_answer()
                choice = _yes_no(answer) if answer is not None else None
                if choice is None:
                    if answer is not None:
                        self.used -= 1
                    yield "reply", self.prompt(node["ask"], "question", YES_NO)
                    return
                self.trail.append({"label": node.get("short") or node["ask"], "answer": choice.capitalize()})
                node_id = node[choice]
            elif "identify_site" in node:
                found = yield from self.identify(node)
                if found is PENDING:
                    return
                node_id = node["found" if found else "not_found"]
            elif "check" in node:
                hit = yield from self.check(node)
                node_id = node["yes" if hit else "no"]
            else:
                yield "reply", self.outcome(node_id, node)
                return
        raise FlowError("The guided check did not finish.")

    def tool(self, name: str, args: dict, flow: bool = False):
        yield "step", {"tool": name, "state": "started"}
        output = self.ctx.execute(name, args, flow=flow)
        yield "step", {"tool": name, "state": "failed" if "error" in output else "done"}
        return output

    def identify(self, node: dict):
        output = yield from self.tool("resolve_sites", {"query": "*"})
        sites = output.get("data") or []
        attempts, limit = 0, int(node.get("attempts", 3))
        while sites and attempts < limit:
            text = self.next_answer()
            if text is None:
                options = [{"label": site["name"], "value": site["name"]} for site in sites]
                text_prompt = node["identify_site"] if attempts == 0 else node.get("retry", DEFAULT_RETRY)
                yield "reply", self.prompt(text_prompt, "site", options, free_text=True)
                return PENDING
            site = _exact_site(text, sites)
            if site is None and (guess := _closest_site(text, sites)) is not None:
                confirmation = self.next_answer()
                if confirmation is None:
                    yield "reply", self.prompt(node.get("confirm", DEFAULT_CONFIRM).format(site=guess["name"]),
                                               "confirm", YES_NO)
                    return PENDING
                site = guess if _yes_no(confirmation) == "yes" else None
            if site is not None:
                self.site = {"id": site["id"], "name": site["name"]}
                self.trail.append({"label": "Site", "answer": site["name"]})
                return True
            attempts += 1
        self.trail.append({"label": "Site", "answer": "Not identified"})
        return False

    def check(self, node: dict):
        site = self.site or self.single_site()
        if site is None:
            raise FlowError("A check needs a site; identify the site first.")
        output = yield from self.tool(CHECK_TOOLS[node["check"]], {"site_id": site["id"]}, flow=True)
        label = node.get("short") or "Open health check"
        if "error" in output:
            self.trail.append({"label": label, "answer": "Couldn't check"})
            return False
        self.source_ids.append(output["source_id"])
        wanted = set(node["any_open"])
        found = [item["check"] for item in output["data"]
                 if item.get("status") == "open" and item.get("type") in wanted]
        self.trail.append({"label": label, "answer": ", ".join(found) if found else "None open"})
        return bool(found)

    def outcome(self, node_id: str, node: dict) -> dict:
        kind = node.get("kind", "resolve")
        frame = self.frame("outcome", outcome=kind, trail=self.trail)
        if kind == "ticket":
            # Categories only: the backlog shows which flow endings still need people.
            self.ctx.store.record_unanswered(self.ctx.session_id, f"flow_ticket:{self.flow_id}:{node_id}")
        site = self.site or self.single_site()
        steps = "\n".join(f"{index}. {item['label']}: {item['answer']}" for index, item in enumerate(self.trail, 1))
        frame["ticket"] = {
            "site_id": site["id"] if site else "",
            "summary": self.flow["title"][:500],
            "steps_tried": (f"Guided check: {self.flow['title']}\n{steps}" if steps else self.flow["title"])[:2000],
        }
        return _reply("answer", node["outcome"], self.ctx, frame, self.source_ids)
