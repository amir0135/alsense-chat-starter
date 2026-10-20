"""Demo scenarios: repeatable walkthroughs that a presenter can start and reset without notes.

The shipped `scenarios.yaml` uses only synthetic data. Point SCENARIOS_FILE at a `*.local.yaml` file to demo
customer flows; Git, Docker and the export exclude it.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .adapters.synthetic.faults import FAULTS
from .config import ROOT
from .flows import available

SAMPLE_SCENARIOS = ROOT / "scenarios.yaml"
ACTIONS = ("say", "answer", "open", "click", "set")


class ScenarioError(ValueError):
    pass


class Step(BaseModel):
    """One presenter action. Exactly one of say, answer, open, click or set."""

    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1, max_length=300)
    say: str | None = Field(default=None, min_length=1, max_length=6000)
    display: str | None = Field(default=None, min_length=1, max_length=200)
    answer: str | None = Field(default=None, min_length=1, max_length=200)
    label: str | None = Field(default=None, min_length=1, max_length=200)
    open: Literal["report", "ticket"] | None = None
    prefill: dict[str, str] | None = None
    click: Literal["prepare", "confirm", "edit", "status", "support"] | None = None
    set: dict[Literal["format", "report_type", "period"], str] | None = None

    @model_validator(mode="after")
    def one_action(self):
        chosen = [name for name in ACTIONS if getattr(self, name) is not None]
        if len(chosen) != 1:
            raise ValueError(f"each step needs exactly one of {', '.join(ACTIONS)}")
        for extra, owner in (("display", "say"), ("label", "answer"), ("prefill", "open")):
            if getattr(self, extra) is not None and getattr(self, owner) is None:
                raise ValueError(f"{extra} only applies to {owner} steps")
        return self


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    title: str = Field(min_length=1, max_length=120)
    group: Literal["journey", "failure"]
    demonstrates: str = Field(min_length=1, max_length=400)
    persona: str
    faults: list[str] = Field(default_factory=list)
    requires_flows: list[str] = Field(default_factory=list)
    requires_features: list[Literal["reports", "troubleshooting", "support"]] = Field(default_factory=list)
    expected: str = Field(min_length=1, max_length=400)
    steps: list[Step] = Field(min_length=1, max_length=20)


def load_scenarios(path: str | Path = "", personas: set[str] | None = None) -> list[Scenario]:
    source = Path(path or SAMPLE_SCENARIOS)
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{source.name}: not valid YAML ({exc.__class__.__name__}).") from None
    raw = data.get("scenarios") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ScenarioError(f"{source.name}: needs a non-empty 'scenarios' list.")
    scenarios: list[Scenario] = []
    problems: list[str] = []
    for index, item in enumerate(raw, 1):
        name = item.get("id", f"#{index}") if isinstance(item, dict) else f"#{index}"
        try:
            scenario = Scenario.model_validate(item)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            problems.append(f"scenario {name}: {where + ': ' if where else ''}{first['msg']}")
            continue
        if personas is not None and scenario.persona not in personas:
            problems.append(f"scenario {name}: unknown persona {scenario.persona!r}.")
        unknown = sorted(set(scenario.faults) - set(FAULTS))
        if unknown:
            problems.append(f"scenario {name}: unknown faults {unknown}.")
        if any(existing.id == scenario.id for existing in scenarios):
            problems.append(f"scenario {name}: duplicate id.")
        scenarios.append(scenario)
    if problems:
        raise ScenarioError(f"{source.name} has {len(problems)} problem(s):\n- " + "\n- ".join(problems))
    return scenarios


def unavailable_reason(
    scenario: Scenario, flows: dict, features: frozenset[str], data_source: str, personas: set[str] | None = None
) -> str:
    """Why a scenario can't run in this configuration, or an empty string when it can."""
    if personas is not None and scenario.persona not in personas:
        return f"Needs the demo user {scenario.persona!r}, which this sign-in doesn't offer."
    missing = [flow_id for flow_id in scenario.requires_flows if flow_id not in flows or not available(flows[flow_id])]
    if missing:
        return f"Needs the guided check {', '.join(missing)}, which isn't loaded or approved for demos."
    off = [feature for feature in scenario.requires_features if feature not in features]
    if off:
        return f"Needs {', '.join(off)}, which this configuration turns off."
    if scenario.faults and data_source != "synthetic":
        return "Synthetic faults need DATA_SOURCE=synthetic."
    return ""
