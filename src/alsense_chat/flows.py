"""Guided troubleshooting: deterministic decision trees that operations teams own and the backend runs.

Flows are versioned YAML data, validated when they load. Each request replays a flow from its start with the
user's answers so far, so no flow state is stored and every lookup is re-checked with the current session's
permissions. A flow can only ask, identify a site, run a backend lookup and end with guidance or a prefilled
support request; nothing in a flow can submit anything.
"""

import difflib
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

from .config import ROOT
from .explain import explain, humanize
from .tools import FEATURE_OFF, FLOW_TOOLS, Context

SAMPLE_FLOWS = ROOT / "troubleshooting.yaml"
NODE_KINDS = ("ask", "identify_site", "check", "outcome")
BRANCHES = {"ask": ("yes", "no"), "identify_site": ("found", "not_found"), "check": ("yes", "no"), "outcome": ()}
OPTIONAL_BRANCHES = {"check": ("unavailable",)}
NODE_KEYS = {
    "ask": {"ask", "short", "yes", "no"},
    "identify_site": {"identify_site", "attempts", "retry", "confirm", "ambiguous", "found", "not_found"},
    "check": {"check", "any_open", "short", "yes", "no", "unavailable"},
    "outcome": {"outcome", "kind", "category"},
}
FLOW_KEYS = {"title", "description", "version", "status", "owner", "approved_for_demo", "triggers", "start", "nodes"}
REQUIRED = ("title", "version", "status", "owner", "approved_for_demo", "start", "nodes")
STATUSES = ("draft", "review", "approved", "retired")
OUTCOME_KINDS = ("resolve", "ticket")
CHECK_TOOLS = {"site_health": "check_site_health"}
SEMVER = re.compile(r"\d+\.\d+\.\d+")
YES_NO = [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]
DEFAULT_RETRY = "I couldn't find that site. Which company and site is it?"
DEFAULT_CONFIRM = "Do you mean site {site}?"
DEFAULT_AMBIGUOUS = "I found more than one matching site. Which one do you mean?"
DEFAULT_FAILURE = (
    "ticket",
    "{message} I can't finish this check without it. Prepare a support request, or try again later.",
)
FAILURES = {
    "forbidden": ("resolve", "{message} Ask your administrator to review your site permissions."),
    "unauthenticated": ("resolve", "{message}"),
    "disabled": ("resolve", "{message}"),
}
MAX_ANSWERS = 40
MAX_ANSWER_LENGTH = 200
PENDING = object()
FAILED = object()

if not set(CHECK_TOOLS.values()) <= set(FLOW_TOOLS):
    raise RuntimeError("Every flow check must map to a backend flow tool.")


class FlowError(ValueError):
    pass


class _Loader(yaml.SafeLoader):
    """Safe YAML that rejects duplicate keys, which PyYAML would otherwise silently overwrite."""


def _mapping(loader: _Loader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in seen
        except TypeError:
            continue
        if duplicate:
            raise FlowError(f"Duplicate key {key!r} on line {key_node.start_mark.line + 1}.")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_flows(path: str | Path = "") -> dict[str, dict]:
    source = Path(path or SAMPLE_FLOWS)
    try:
        data = yaml.load(source.read_text(encoding="utf-8"), Loader=_Loader) or {}
    except yaml.YAMLError as exc:
        raise FlowError(f"{source.name}: not valid YAML ({exc.__class__.__name__}).") from None
    flows = data.get("flows") if isinstance(data, dict) else None
    if not isinstance(flows, dict) or not flows:
        raise FlowError(f"{source.name}: a flows file needs a non-empty 'flows' mapping.")
    problems = [problem for flow_id, flow in flows.items() for problem in flow_problems(flow_id, flow)]
    if problems:
        raise FlowError(f"{source.name} has {len(problems)} problem(s):\n- " + "\n- ".join(problems))
    return flows


def validate_flow(flow_id: str, flow: dict) -> None:
    problems = flow_problems(flow_id, flow)
    if problems:
        raise FlowError("\n".join(problems))


def available(flow: dict) -> bool:
    """Only flows approved for demos are offered; others still load and validate."""
    return flow.get("approved_for_demo") is True and flow.get("status") != "retired"


def flow_problems(flow_id: str, flow: dict) -> list[str]:
    if not isinstance(flow_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", flow_id):
        return [f"Flow id {flow_id!r} must use lowercase letters, digits and hyphens."]
    if not isinstance(flow, dict):
        return [f"{flow_id}: a flow is a mapping."]
    problems = [f"{flow_id}: unsupported key {key!r}." for key in sorted(set(flow) - FLOW_KEYS, key=str)]
    missing = [key for key in REQUIRED if flow.get(key) in (None, "")]
    if missing:
        problems.append(f"{flow_id}: missing required {', '.join(missing)}.")
    if "version" in flow and not (isinstance(flow["version"], str) and SEMVER.fullmatch(flow["version"])):
        problems.append(f'{flow_id}: version must be a semantic version in quotes, such as "1.0.0".')
    if "status" in flow and flow["status"] not in STATUSES:
        problems.append(f"{flow_id}: status must be one of {', '.join(STATUSES)}.")
    if "owner" in flow and not (isinstance(flow["owner"], str) and flow["owner"].strip()):
        problems.append(f"{flow_id}: owner must name the team that approves this flow.")
    if "approved_for_demo" in flow and not isinstance(flow["approved_for_demo"], bool):
        problems.append(f"{flow_id}: approved_for_demo must be true or false.")
    if flow.get("status") == "retired" and flow.get("approved_for_demo") is True:
        problems.append(f"{flow_id}: a retired flow can't be approved for demos.")
    for field in ("title", "description"):
        if field in flow and not isinstance(flow[field], str):
            problems.append(f"{flow_id}: {field} must be text.")
    for trigger in flow.get("triggers") or []:
        if not isinstance(trigger, list) or not trigger or not all(isinstance(t, str) and t.strip() for t in trigger):
            problems.append(f"{flow_id}: each trigger is a non-empty list of words.")
    nodes = flow.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return [*problems, f"{flow_id}: nodes must be a non-empty mapping."]
    if flow.get("start") not in nodes:
        problems.append(f"{flow_id}: start must name a node.")
    linked = True
    for node_id, node in nodes.items():
        where = f"{flow_id}.{node_id}"
        if not isinstance(node, dict):
            problems.append(f"{where}: a node is a mapping.")
            linked = False
            continue
        # YAML 1.1 reads bare yes/no keys as booleans; authors should not have to quote them.
        for flag, name in ((True, "yes"), (False, "no")):
            if flag in node:
                node[name] = node.pop(flag)
        kinds = [kind for kind in NODE_KINDS if kind in node]
        if len(kinds) != 1:
            problems.append(f"{where}: use exactly one of {', '.join(NODE_KINDS)}.")
            linked = False
            continue
        kind = kinds[0]
        problems += [
            f"{where}: unsupported key {key!r} for a {kind} node."
            for key in sorted(set(node) - NODE_KEYS[kind], key=str)
        ]
        if not isinstance(node[kind], str) or not node[kind].strip():
            problems.append(f"{where}: {kind} needs text.")
        for branch in BRANCHES[kind]:
            if node.get(branch) not in nodes:
                problems.append(f"{where}: '{branch}' must name a node.")
                linked = False
        for branch in OPTIONAL_BRANCHES.get(kind, ()):
            if branch in node and node[branch] not in nodes:
                problems.append(f"{where}: '{branch}' must name a node.")
                linked = False
        problems += _node_problems(where, kind, node)
    if linked and flow.get("start") in nodes:
        problems += _graph_problems(flow_id, nodes, flow["start"])
    return problems


def _node_problems(where: str, kind: str, node: dict) -> list[str]:
    problems = []
    if kind == "check":
        if node["check"] not in CHECK_TOOLS:
            problems.append(f"{where}: check must be one of {sorted(CHECK_TOOLS)}.")
        wanted = node.get("any_open")
        if not (isinstance(wanted, list) and wanted and all(isinstance(item, str) and item for item in wanted)):
            problems.append(f"{where}: any_open must list the open check types that count as a match.")
    if kind == "outcome":
        if node.get("kind") not in OUTCOME_KINDS:
            problems.append(f"{where}: outcome kind must be resolve or ticket.")
        if "category" in node and not (
            isinstance(node["category"], str) and re.fullmatch(r"[a-z0-9_]+", node["category"])
        ):
            problems.append(f"{where}: category uses lowercase letters, digits and underscores.")
    if kind == "identify_site":
        if "{site}" not in str(node.get("confirm", DEFAULT_CONFIRM)):
            problems.append(f"{where}: confirm text must contain {{site}}.")
        attempts = node.get("attempts", 3)
        if not (isinstance(attempts, int) and not isinstance(attempts, bool) and 1 <= attempts <= 5):
            problems.append(f"{where}: attempts must be a whole number from 1 to 5.")
    return problems


def _graph_problems(flow_id: str, nodes: dict, start: str) -> list[str]:
    """Flows must be acyclic, so replaying the answers always ends, and every node must be reachable."""
    problems: list[str] = []
    finished: set[str] = set()
    path: list[str] = []

    def visit(node_id: str) -> None:
        if node_id in path:
            message = f"{flow_id}: the flow loops back to {node_id}."
            if message not in problems:
                problems.append(message)
            return
        if node_id in finished:
            return
        path.append(node_id)
        node = nodes[node_id]
        kind = next(kind for kind in NODE_KINDS if kind in node)
        for branch in (*BRANCHES[kind], *OPTIONAL_BRANCHES.get(kind, ())):
            if branch in node:
                visit(node[branch])
        path.pop()
        finished.add(node_id)

    visit(start)
    unreachable = [str(node_id) for node_id in nodes if node_id not in finished]
    if unreachable:
        problems.append(f"{flow_id}: unreachable nodes {', '.join(unreachable)}.")
    return problems


def match_trigger(flows: dict, message: str) -> str | None:
    text = message.casefold()
    for flow_id, flow in flows.items():
        if available(flow) and any(
            all(term.casefold() in text for term in trigger) for trigger in flow.get("triggers", [])
        ):
            return flow_id
    return None


def _reply(
    status: str,
    text: str,
    ctx: Context,
    flow: dict,
    source_ids: list[str] | None = None,
    why: list[dict] | None = None,
) -> dict:
    # Flow text is authored by the content owner, not generated, so it skips the model reply guard.
    return {
        "status": status,
        "text": text,
        "source_ids": list(source_ids or []),
        "sources": ctx.evidence,
        "actions": [],
        "synthetic": ctx.services.kind == "synthetic",
        "flow": flow,
        "why": why or explain(ctx, checks="Approved flow wording"),
    }


def menu_reply(ctx: Context) -> dict:
    ctx.assistant = "guided"
    if "troubleshooting" not in ctx.features:
        return _reply("out_of_scope", FEATURE_OFF["troubleshooting"], ctx, {"kind": "menu", "options": []})
    options = [
        {"value": flow_id, "label": flow["title"], "description": flow.get("description", "")}
        for flow_id, flow in ctx.flows.items()
        if available(flow)
    ]
    if not options:
        return _reply(
            "out_of_scope", "Guided checks aren't configured in this demo.", ctx, {"kind": "menu", "options": []}
        )
    return _reply("clarification", "What do you need help with?", ctx, {"kind": "menu", "options": options})


def run_flow(flow_id: str, answers: list, ctx: Context) -> Iterator[tuple[str, dict]]:
    ctx.assistant = "guided"
    if "troubleshooting" not in ctx.features:
        yield "reply", _reply("out_of_scope", FEATURE_OFF["troubleshooting"], ctx, {"kind": "menu", "options": []})
        return
    flow = ctx.flows.get(flow_id)
    if flow is None or not available(flow):
        raise FlowError("Unknown guided check.")
    if (
        not isinstance(answers, list)
        or len(answers) > MAX_ANSWERS
        or not all(isinstance(answer, str) and len(answer) <= MAX_ANSWER_LENGTH for answer in answers)
    ):
        raise FlowError("Invalid answers.")
    yield from _Walk(flow_id, flow, answers, ctx).run()


def _yes_no(answer: str) -> str | None:
    return {"yes": "yes", "no": "no"}.get(answer.strip().casefold())


def _exact_site(text: str, sites: list[dict]) -> dict | None:
    folded = " ".join(text.casefold().split())
    matches = [site for site in sites if site["name"].casefold() in folded]
    return max(matches, key=lambda site: len(site["name"])) if matches else None


def _candidates(text: str, sites: list[dict]) -> list[dict]:
    """Sites that plausibly match a description; several equally good matches are ambiguous."""
    folded = " ".join(text.casefold().split())
    if len(folded) < 3:
        return []
    scored = []
    for site in sites:
        name = site["name"].casefold()
        score = difflib.SequenceMatcher(None, folded, name).ratio()
        if folded in name:
            score = max(score, 0.75)
        for word in folded.split():
            if len(word) >= 4 and any(
                difflib.SequenceMatcher(None, word, part).ratio() >= 0.8 for part in name.split()
            ):
                score = max(score, 0.65)
        if score >= 0.6:
            scored.append((score, site))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return [site for score, site in scored if best - score < 0.05]


class _Walk:
    def __init__(self, flow_id: str, flow: dict, answers: list[str], ctx: Context):
        self.flow_id, self.flow, self.ctx = flow_id, flow, ctx
        self.answers = answers
        self.used = 0
        self.trail: list[dict] = []
        self.site: dict | None = None
        self.source_ids: list[str] = []
        self.health = ""
        self.failure: dict = {}

    def next_answer(self) -> str | None:
        if self.used >= len(self.answers):
            return None
        self.used += 1
        return self.answers[self.used - 1].strip()

    def frame(self, kind: str, **extra) -> dict:
        return {
            "id": self.flow_id,
            "title": self.flow["title"],
            "version": self.flow["version"],
            "kind": kind,
            "step": self.used + 1,
            "answers": self.answers[: self.used],
            **extra,
        }

    def why(self, outcome: str = "") -> list[dict]:
        rows = [("Flow", f"{self.flow['title']} · v{self.flow['version']}")]
        if self.site:
            rows.append(("Site resolved", self.site["name"]))
        if self.health:
            rows.append(("Health result", self.health))
        if outcome:
            rows.append(("Outcome", outcome))
        return explain(self.ctx, rows, checks="Approved flow wording")

    def prompt(self, text: str, kind: str, options: list[dict], free_text: bool = False) -> dict:
        frame = self.frame(kind, options=options, free_text=free_text)
        return _reply("clarification", text, self.ctx, frame, self.source_ids, self.why())

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
                if found is FAILED:
                    yield "reply", self.failed_step()
                    return
                node_id = node["found" if found else "not_found"]
            elif "check" in node:
                result = yield from self.check(node)
                if result == "unavailable":
                    if "unavailable" not in node:
                        yield "reply", self.failed_step()
                        return
                    node_id = node["unavailable"]
                else:
                    node_id = node[result]
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
        if "error" in output:
            self.failure = output
            self.trail.append({"label": "Site", "answer": "Couldn't look up your sites"})
            return FAILED
        sites = output["data"]
        attempts, limit = 0, int(node.get("attempts", 3))
        while sites and attempts < limit:
            text = self.next_answer()
            if text is None:
                options = [{"label": site["name"], "value": site["name"]} for site in sites]
                text_prompt = node["identify_site"] if attempts == 0 else node.get("retry", DEFAULT_RETRY)
                yield "reply", self.prompt(text_prompt, "site", options, free_text=True)
                return PENDING
            site = _exact_site(text, sites)
            if site is None:
                candidates = _candidates(text, sites)
                if len(candidates) > 1:
                    choice = self.next_answer()
                    if choice is None:
                        options = [{"label": item["name"], "value": item["name"]} for item in candidates]
                        yield "reply", self.prompt(node.get("ambiguous", DEFAULT_AMBIGUOUS), "site", options, True)
                        return PENDING
                    site = _exact_site(choice, candidates)
                elif candidates:
                    guess = candidates[0]
                    confirmation = self.next_answer()
                    if confirmation is None:
                        question = node.get("confirm", DEFAULT_CONFIRM).format(site=guess["name"])
                        yield "reply", self.prompt(question, "confirm", YES_NO)
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
        self.site = site
        output = yield from self.tool(CHECK_TOOLS[node["check"]], {"site_id": site["id"]}, flow=True)
        label = node.get("short") or "Open health check"
        if "error" in output:
            self.failure = output
            self.trail.append({"label": label, "answer": "Couldn't check"})
            self.health = f"Couldn't check: {humanize(output['category']).lower()}"
            return "unavailable"
        self.source_ids.append(output["source_id"])
        wanted = set(node["any_open"])
        found = [
            item["check"]
            for item in output["data"]["checks"]
            if item.get("status") == "open" and item.get("type") in wanted
        ]
        self.trail.append({"label": label, "answer": ", ".join(found) if found else "None open"})
        self.health = ", ".join(found) if found else "No matching open checks"
        return "yes" if found else "no"

    def ticket(self) -> dict:
        site = self.site or self.single_site()
        steps = "\n".join(f"{index}. {item['label']}: {item['answer']}" for index, item in enumerate(self.trail, 1))
        return {
            "site_id": site["id"] if site else "",
            "summary": self.flow["title"][:500],
            "steps_tried": (f"Guided check: {self.flow['title']}\n{steps}" if steps else self.flow["title"])[:2000],
        }

    def failed_step(self) -> dict:
        """A lookup failed: say exactly why instead of guessing a branch."""
        category = self.failure["category"]
        kind, template = FAILURES.get(category, DEFAULT_FAILURE)
        if kind == "ticket":
            self.ctx.store.record_unanswered(self.ctx.session_id, f"flow_failed:{self.flow_id}:{category}")
        frame = self.frame(
            "outcome", outcome=kind, category=f"lookup_{category}", trail=self.trail, ticket=self.ticket()
        )
        why = self.why(f"Stopped · {humanize(category).lower()}")
        reply = _reply("error", template.format(message=self.failure["error"]), self.ctx, frame, self.source_ids, why)
        reply["error"] = {"category": category, "service": self.failure.get("service", "")}
        return reply

    def outcome(self, node_id: str, node: dict) -> dict:
        kind = node["kind"]
        category = node.get("category") or str(node_id).replace("-", "_")
        if kind == "ticket":
            # Categories only: the backlog shows which flow endings still need people.
            self.ctx.store.record_unanswered(self.ctx.session_id, f"flow_ticket:{self.flow_id}:{node_id}")
        frame = self.frame("outcome", outcome=kind, category=category, trail=self.trail, ticket=self.ticket())
        label = "Support request suggested" if kind == "ticket" else "Guidance for you"
        return _reply(
            "answer",
            node["outcome"],
            self.ctx,
            frame,
            self.source_ids,
            self.why(f"{label} · {humanize(category).lower()}"),
        )
