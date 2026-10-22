"""Modular agent configuration: core instructions plus one folder per domain.

    agent/core/instructions.yaml          always sent to the model
    agent/<domain>/instructions.yaml      purpose, routing words, slash commands, dependencies, instructions
    agent/<domain>/tools.yaml             the domain's operations and who may call them

For each request, `Agent.route` picks the domains it needs, and only their model tools are offered. A domain
with `status: future` is a placeholder and is never offered. Handlers for the tools live in `alsense_chat.domains`
and `alsense_chat.tools`, which check at startup that every declared tool has a matching schema.
"""

import functools
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from ..config import FEATURES

AGENT_ROOT = Path(__file__).resolve().parent
NAME = re.compile(r"[a-z][a-z0-9_]*")


class AgentConfigError(ValueError):
    pass


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CoreConfig(_Config):
    product: str
    scope: str
    domains: list[str]
    default_domains: list[str]
    instructions: str


class DomainConfig(_Config):
    domain: str
    title: str
    status: Literal["active", "future"]
    description: str
    requires: list[str] = []
    routing: list[list[str]] = []
    commands: list[str] = []
    instructions: str = ""


class ToolConfig(_Config):
    caller: Literal["model", "user", "backend"]
    feature: Literal["reports", "troubleshooting", "support"] | None = None
    description: str
    parameters: dict | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    domain: str
    caller: str
    feature: str | None
    description: str
    parameters: dict | None


@dataclass(frozen=True)
class Module:
    config: DomainConfig
    tools: dict[str, Tool]

    @property
    def name(self) -> str:
        return self.config.domain

    @property
    def title(self) -> str:
        return self.config.title

    @property
    def active(self) -> bool:
        return self.config.status == "active"


@dataclass(frozen=True)
class Routing:
    domains: tuple[str, ...]
    tools: tuple[str, ...]
    reason: Literal["command", "matched", "previous", "default"]


def _read(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AgentConfigError(f"{path}: not valid YAML ({exc.__class__.__name__}).") from None
    if not isinstance(data, dict):
        raise AgentConfigError(f"{path}: expected a mapping.")
    return data


def _parse(model: type[_Config], data: dict, where: str):
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        raise AgentConfigError(f"{where}: {location + ': ' if location else ''}{first['msg']}") from None


def _check_parameters(where: str, parameters: dict | None) -> None:
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise AgentConfigError(f"{where}: parameters must be an object schema.")
    properties = parameters.get("properties")
    required = parameters.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list) or not set(required) <= set(properties):
        raise AgentConfigError(f"{where}: parameters need properties and a required list naming some of them.")
    if parameters.get("additionalProperties") is not False:
        raise AgentConfigError(f"{where}: parameters must set additionalProperties: false.")


class Agent:
    def __init__(self, core: CoreConfig, modules: dict[str, Module]):
        self.core = core
        self.modules = modules

    def active(self) -> list[Module]:
        return [module for module in self.modules.values() if module.active]

    def future(self) -> list[Module]:
        return [module for module in self.modules.values() if not module.active]

    def tools(self, caller: str | None = None) -> dict[str, Tool]:
        return {
            name: tool
            for module in self.modules.values()
            for name, tool in module.tools.items()
            if caller is None or tool.caller == caller
        }

    def with_requirements(self, domains: Iterable[str]) -> tuple[str, ...]:
        selected = set()
        pending = [domain for domain in domains if domain in self.modules and self.modules[domain].active]
        while pending:
            domain = pending.pop()
            if domain not in selected:
                selected.add(domain)
                pending.extend(self.modules[domain].config.requires)
        return tuple(domain for domain in self.core.domains if domain in selected)

    def model_tools(self, domains: Iterable[str], features: frozenset[str] = FEATURES) -> tuple[str, ...]:
        return tuple(
            name
            for domain in domains
            for name, tool in self.modules[domain].tools.items()
            if tool.caller == "model" and (tool.feature is None or tool.feature in features)
        )

    def route(self, message: str, previous: Iterable[str] = (), features: frozenset[str] = FEATURES) -> Routing:
        """Domains a request needs: a slash command's domain, routing words, the previous turn's, or the default."""
        text = message.strip().casefold()
        command = text.split(" ", 1)[0] if text.startswith("/") else ""
        reason: Literal["command", "matched", "previous", "default"]
        if command:
            chosen = [module.name for module in self.active() if command in module.config.commands]
            reason = "command"
        else:
            chosen = [
                module.name
                for module in self.active()
                if any(
                    all(re.search(rf"(?<!\w){re.escape(term.casefold())}", text) for term in words)
                    for words in module.config.routing
                )
            ]
            reason = "matched"
        if not chosen:
            chosen = [domain for domain in previous if domain in self.modules and self.modules[domain].active]
            reason = "previous"
        if not chosen:
            chosen, reason = list(self.core.default_domains), "default"
        domains = self.with_requirements(chosen)
        return Routing(domains=domains, tools=self.model_tools(domains, features), reason=reason)

    def instructions(self, domains: Iterable[str]) -> str:
        parts = [self.core.instructions.strip()]
        parts += [self.modules[domain].config.instructions.strip() for domain in domains]
        return "\n\n".join(part for part in parts if part)

    def titles(self, domains: Iterable[str]) -> list[str]:
        return [self.modules[domain].title for domain in domains]


def load_agent(root: Path | None = None) -> Agent:
    return _load(Path(root or AGENT_ROOT).resolve())


@functools.cache
def _load(root: Path) -> Agent:
    core = _parse(CoreConfig, _read(root / "core" / "instructions.yaml"), "core/instructions.yaml")
    folders = {path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith(("_", "."))}
    folders.discard("core")
    if set(core.domains) != folders or len(core.domains) != len(set(core.domains)):
        raise AgentConfigError(f"core domains {core.domains} must list each domain folder once: {sorted(folders)}.")
    modules: dict[str, Module] = {}
    seen: dict[str, str] = {}
    for domain in core.domains:
        where = f"{domain}/instructions.yaml"
        config = _parse(DomainConfig, _read(root / domain / "instructions.yaml"), where)
        if config.domain != domain or not NAME.fullmatch(domain):
            raise AgentConfigError(f"{where}: domain must be '{domain}'.")
        tools_file = root / domain / "tools.yaml"
        if config.status == "future":
            if tools_file.exists() or config.routing or config.commands or config.instructions.strip():
                raise AgentConfigError(f"{where}: a future module has a description only, no tools or routing.")
            modules[domain] = Module(config, {})
            continue
        if not config.instructions.strip() or not tools_file.exists():
            raise AgentConfigError(f"{domain}: an active module needs instructions and a tools.yaml.")
        raw_tools = _read(tools_file).get("tools")
        if not isinstance(raw_tools, dict) or not raw_tools:
            raise AgentConfigError(f"{domain}/tools.yaml: needs a non-empty 'tools' mapping.")
        tools: dict[str, Tool] = {}
        for name, raw in raw_tools.items():
            if not isinstance(name, str) or not NAME.fullmatch(name):
                raise AgentConfigError(f"{domain}/tools.yaml: tool name {name!r} must be snake_case.")
            if name in seen:
                raise AgentConfigError(f"{domain}/tools.yaml: {name} is already declared by {seen[name]}.")
            tool = _parse(ToolConfig, raw if isinstance(raw, dict) else {}, f"{domain}/tools.yaml: {name}")
            if tool.caller != "user":
                _check_parameters(f"{domain}/tools.yaml: {name}", tool.parameters)
            seen[name] = domain
            tools[name] = Tool(name, domain, tool.caller, tool.feature, tool.description.strip(), tool.parameters)
        modules[domain] = Module(config, tools)
    active = {name for name, module in modules.items() if module.active}
    for module in modules.values():
        unknown = set(module.config.requires) - (active - {module.name})
        if unknown:
            raise AgentConfigError(f"{module.name}: requires unknown or inactive domains {sorted(unknown)}.")
    if not set(core.default_domains) <= active:
        raise AgentConfigError("core default_domains must name active domains.")
    return Agent(core, modules)
