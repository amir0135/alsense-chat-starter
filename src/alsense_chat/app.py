import hmac
import json
import logging
import re
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .adapters import IntegrationError, Services, build_services
from .adapters.contracts import User
from .adapters.interfaces import PersonaDirectory
from .adapters.synthetic import FAULTS, with_faults
from .config import ROOT, Settings, load_settings
from .flows import load_flows
from .readiness import app_version, system_status
from .runner import FoundryRunner, demo_run, error_reply
from .scenarios import load_scenarios, unavailable_reason
from .store import MissingRecord, Store
from .tools import FEATURE_OFF, Context, model_definition

log = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)
CORRELATION_ID = re.compile(r"[A-Za-z0-9-]{8,64}")


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona: Literal["alice", "bob", "carol", "external"]
    access_code: str = Field(default="", max_length=256)


class ScenarioStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_code: str = Field(default="", max_length=256)


class Chat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=6000)


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


class CorrelationMiddleware:
    """Give every request a correlation id (reusing a well-formed X-Correlation-ID) and echo it back."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(b"x-correlation-id", b"").decode("latin-1")
        correlation_id = incoming if CORRELATION_ID.fullmatch(incoming) else uuid.uuid4().hex[:16]
        scope.setdefault("state", {})["correlation_id"] = correlation_id

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"x-correlation-id", correlation_id.encode())]
            await send(message)

        await self.app(scope, receive, send_with_id)


def configure_logging(level: str) -> None:
    logger = logging.getLogger("alsense_chat")
    logger.setLevel(level)
    if not any(getattr(handler, "alsense_chat", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.alsense_chat = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
        logger.propagate = False


def context(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Context:
    state = request.app.state
    if not state.settings.enabled:
        raise HTTPException(403, "Chat is disabled by the backend feature flag.")
    if credentials is None:
        raise HTTPException(401, "A bearer session token is required.")
    try:
        session = state.store.authenticate(credentials.credentials)
    except MissingRecord as exc:
        raise HTTPException(401, str(exc)) from None
    if not session.user.internal:
        raise HTTPException(403, "Chat is restricted to internal users.")
    faults = state.settings.synthetic_faults | session.faults
    return Context(
        with_faults(state.services, faults),
        state.store,
        session.id,
        session.user,
        flows=state.flows,
        features=state.settings.features,
        faults=faults,
        session_faults=session.faults,
        correlation_id=request.state.correlation_id,
        scenario=session.scenario,
    )


def audit_reply(ctx: Context, reply: dict | None) -> None:
    flow = (reply or {}).get("flow") or {}
    error = (reply or {}).get("error") or {}
    status = (reply or {}).get("status", "")
    if reply is None:
        outcome, category = "runtime_error", "runtime_error"
    elif flow.get("kind") == "outcome":
        outcome, category = f"{flow['outcome']}:{flow['category']}", f"flow:{flow['kind']}"
    else:
        outcome, category = status, f"flow:{flow['kind']}" if flow else status
    ctx.store.audit(
        correlation_id=ctx.correlation_id,
        session_id=ctx.session_id,
        persona=ctx.user.id,
        scenario=ctx.scenario or None,
        event="flow" if flow.get("id") else "chat",
        flow_id=flow.get("id"),
        flow_version=flow.get("version"),
        tools=[{"tool": call["tool"], "result": call["result"]} for call in ctx.calls],
        inputs=[{"tool": call["tool"], **call["inputs"]} for call in ctx.calls if call["inputs"]],
        response_category=f"error:{error['category']}" if error else category,
        confirmation="prepared" if ctx.actions else "none",
        outcome=outcome,
        error_category=error.get("category") or (None if reply else "runtime_error"),
    )
    log.info("chat %s persona=%s outcome=%s", ctx.correlation_id, ctx.user.id, outcome)


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.validate()
    model_definition()
    configure_logging(settings.log_level)
    if settings.reset_on_start:
        removed = Store.reset(settings.db)
        log.warning("RESET_DEMO_DATA_ON_START: removed %s", ", ".join(removed) or "no existing demo data")
    services = services or build_services(settings.data_source)
    # A real identity provider offers no demo personas: scenarios then load but report themselves unavailable.
    directory = isinstance(services.identity, PersonaDirectory)
    personas = services.identity.personas() if directory else []  # type: ignore[attr-defined]
    persona_ids = {persona["id"] for persona in personas}
    flows = load_flows(settings.flows_file)
    scenarios = load_scenarios(settings.scenarios_file, persona_ids if directory else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.telemetry_connection:
            from azure.monitor.opentelemetry import configure_azure_monitor

            configure_azure_monitor(
                connection_string=settings.telemetry_connection,
                logger_name="alsense_chat",
                instrumentation_options={"fastapi": {"enabled": True}},
            )
        app.state.runner = FoundryRunner(settings) if settings.agent_mode == "foundry" else None
        log.info(
            "Ready: assistant=%s data=%s flows=%s scenarios=%d faults=%s",
            settings.agent_mode,
            services.kind,
            "local file" if settings.flows_file else "synthetic sample",
            len(scenarios),
            ",".join(sorted(settings.synthetic_faults)) or "none",
        )
        yield
        if app.state.runner:
            app.state.runner.close()

    app = FastAPI(title="Alsense synthetic reference starter", version=app_version(), lifespan=lifespan)
    app.add_middleware(CorrelationMiddleware)
    app.state.settings = settings
    app.state.services = services
    app.state.store = Store(settings.db)
    app.state.flows = flows
    app.state.scenarios = scenarios
    app.state.persona_names = [persona["name"] for persona in personas]
    app.state.persona_ids = persona_ids

    @app.exception_handler(IntegrationError)
    def integration_error(request: Request, exc: IntegrationError):
        return JSONResponse(
            {"detail": exc.user_message, "category": exc.category},
            status_code=exc.http_status,
            headers={"X-Error-Category": exc.category},
        )

    def start_session(user: User, faults: frozenset[str] = frozenset(), scenario: str = "") -> dict:
        if not user.internal:
            raise HTTPException(403, "Chat is restricted to internal users.")
        token, session_id = app.state.store.create_session(user, settings.ttl_seconds, faults, scenario)
        return {"token": token, "session_id": session_id, "user": user.model_dump(), "scenario": scenario}

    def check_access_code(supplied: str) -> None:
        if settings.cloud_demo and not hmac.compare_digest(supplied.encode(), settings.demo_access_code.encode()):
            raise HTTPException(401, "Demo access code invalid.")

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "mode": settings.agent_mode,
            "data_source": services.kind,
            "access_code_required": settings.cloud_demo,
            "features": sorted(settings.features),
            "version": app_version(),
            "production_ready": False,
        }

    @app.get("/api/system")
    def system(request: Request):
        return system_status(request.app.state)

    @app.get("/api/demo/personas")
    def demo_personas():
        if not settings.demo_login:
            raise HTTPException(403, "Demo login unavailable.")
        return personas

    @app.get("/api/scenarios")
    def list_scenarios():
        names = {persona["id"]: persona for persona in personas}
        return [
            {
                **scenario.model_dump(),
                "fault_labels": [FAULTS[fault] for fault in scenario.faults],
                "persona_name": names.get(scenario.persona, {}).get("name", scenario.persona),
                "persona_sites": names.get(scenario.persona, {}).get("sites", []),
                "unavailable_reason": unavailable_reason(
                    scenario, flows, settings.features, services.kind, persona_ids
                ),
            }
            for scenario in scenarios
        ]

    @app.post("/api/demo/scenarios/{scenario_id}/start")
    def start_scenario(
        scenario_id: str,
        body: ScenarioStart,
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if not settings.demo_login or not settings.enabled:
            raise HTTPException(403, "Demo login unavailable.")
        if settings.cloud_demo and credentials is not None:
            try:
                app.state.store.authenticate(credentials.credentials)
            except MissingRecord:
                check_access_code(body.access_code)
        else:
            check_access_code(body.access_code)
        scenario = next((item for item in scenarios if item.id == scenario_id), None)
        if scenario is None:
            raise HTTPException(404, "Scenario unavailable.")
        reason = unavailable_reason(scenario, flows, settings.features, services.kind, persona_ids)
        if reason:
            raise HTTPException(409, reason)
        session = start_session(services.identity.users_me(scenario.persona), frozenset(scenario.faults), scenario.id)
        app.state.store.audit(
            correlation_id=request.state.correlation_id,
            session_id=session["session_id"],
            persona=scenario.persona,
            scenario=scenario.id,
            event="scenario_start",
            inputs=[{"faults": sorted(scenario.faults)}] if scenario.faults else [],
            outcome="started",
        )
        return session

    @app.post("/api/demo/session")
    def login(body: Login, request: Request):
        if not settings.demo_login or not settings.enabled:
            raise HTTPException(403, "Demo login unavailable.")
        check_access_code(body.access_code)
        session = start_session(services.identity.users_me(body.persona))
        app.state.store.audit(
            correlation_id=request.state.correlation_id,
            session_id=session["session_id"],
            persona=body.persona,
            event="login",
            outcome="started",
        )
        return session

    @app.post("/api/session/new")
    def new_session(ctx: Annotated[Context, Depends(context)]):
        # A new conversation keeps the demo setup (scenario and its faults), not the old history.
        return start_session(ctx.user, ctx.session_faults, ctx.scenario)

    @app.get("/api/session")
    def session(ctx: Annotated[Context, Depends(context)]):
        return {"user": ctx.user.model_dump(), "history": ctx.store.history(ctx.session_id), "scenario": ctx.scenario}

    @app.get("/api/report-types")
    def report_types(ctx: Annotated[Context, Depends(context)]):
        if "reports" not in ctx.features:
            raise HTTPException(403, FEATURE_OFF["reports"])
        return [report_type.model_dump() for report_type in ctx.services.reports.report_types(ctx.user)]

    @app.post("/api/chat")
    def chat(body: Chat, request: Request, ctx: Annotated[Context, Depends(context)]):
        runner = request.app.state.runner
        # Keep the reference form commands deterministic in model mode too.
        use_model = runner is not None and not body.message.startswith("/")
        ctx.assistant = "model" if use_model else "scripted"

        def events() -> Iterator[str]:
            reply = None
            try:
                run = runner.run(body.message, ctx) if use_model else demo_run(body.message, ctx)
                try:
                    for kind, data in run:
                        if kind == "reply":
                            reply = data
                            ctx.store.add_turn(ctx.session_id, body.message, data)
                        yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"
                except IntegrationError as exc:
                    reply = error_reply(exc, ctx)
                    ctx.store.add_turn(ctx.session_id, body.message, reply)
                    yield f"event: reply\ndata: {json.dumps(reply)}\n\n"
            except Exception:
                log.exception("Chat %s failed; no unverified answer released", ctx.correlation_id)
                ctx.store.record_unanswered(ctx.session_id, "runtime_error")
                yield 'event: error\ndata: {"message":"The run failed. Check the backend log and retry."}\n\n'
            finally:
                audit_reply(ctx, reply)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.post("/api/actions/{action_id}/submit")
    def submit(action_id: str, body: Confirmation, ctx: Annotated[Context, Depends(context)]):
        kind, outcome, error = "", "not_submitted", None
        try:
            action = ctx.store.action(ctx.session_id, action_id)
            kind = action["kind"]
            feature = "reports" if kind == "report" else "support"
            if feature not in ctx.features:
                error = "disabled"
                raise HTTPException(403, FEATURE_OFF[feature])
            ctx.validate_action(kind, action["payload"])
            result = ctx.store.submit(ctx.session_id, action_id, ctx.submit_action)
            outcome = result.get("status", "submitted")
            return result
        except MissingRecord:
            error = "not_found"
            raise HTTPException(404, "Action unavailable.") from None
        except IntegrationError as exc:
            error = exc.category
            if exc.category == "forbidden":
                raise HTTPException(403, "Action is no longer permitted.") from None
            # The draft stays prepared and can be confirmed again later.
            raise HTTPException(
                exc.http_status,
                f"Your request was prepared but not submitted. {exc.user_message}",
                headers={"X-Error-Category": exc.category},
            ) from None
        except ValueError:
            error = "invalid_input"
            raise HTTPException(403, "Action is no longer permitted.") from None
        finally:
            ctx.store.audit(
                correlation_id=ctx.correlation_id,
                session_id=ctx.session_id,
                persona=ctx.user.id,
                scenario=ctx.scenario or None,
                event="submit",
                tools=[{"tool": f"submit_{kind or 'unknown'}", "result": f"error:{error}" if error else outcome}],
                confirmation="confirmed",
                outcome=outcome,
                error_category=error,
            )

    @app.get("/reference/{path:path}")
    def reference(path: str):
        return {"synthetic": True, "message": "Placeholder view only; no real Alsense report or ticket was sent."}

    @app.get("/system", include_in_schema=False)
    def system_page():
        return FileResponse(ROOT / "static" / "system.html")

    app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="widget")
    return app
