import hmac
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .adapters import IntegrationError, Services, build_services
from .adapters.contracts import User
from .adapters.interfaces import PersonaDirectory
from .adapters.synthetic import FAULTS, with_faults
from .config import ROOT, Settings, load_settings
from .feedback import Rating, Reason, Vocabulary, answer_context, rateable
from .flows import load_flows
from .readiness import app_version, git_commit, system_status
from .runner import FoundryRunner, demo_run, error_reply
from .scenarios import load_scenarios, unavailable_reason
from .store import DuplicateRecord, MissingRecord, Store
from .timing import Timings
from .tools import FEATURE_OFF, Context, check_registry

log = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)
CORRELATION_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
# Answer ids are secrets.token_urlsafe(16): 22 URL-safe characters.
ANSWER_ID = re.compile(r"[A-Za-z0-9_-]{22}")
# Widget timings: whole milliseconds since the question was sent, up to ten minutes.
ClientMilliseconds = Annotated[int, Field(strict=True, ge=0, le=600_000)]


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


class Feedback(BaseModel):
    """A rating of one answer: a reason from the fixed list may explain a thumbs-down. No free text."""

    model_config = ConfigDict(extra="forbid")
    rating: Rating
    reason: Reason | None = None

    @model_validator(mode="after")
    def reason_explains_a_thumbs_down(self) -> "Feedback":
        if self.reason and self.rating != "down":
            raise ValueError("Only a thumbs-down takes a reason.")
        return self


class ClientTiming(BaseModel):
    """The widget's own timings of one chat request in this session, joined to it by correlation id."""

    model_config = ConfigDict(extra="forbid")
    correlation_id: str = Field(pattern=rf"^{CORRELATION_ID.pattern}$")
    headers: ClientMilliseconds
    first_event: ClientMilliseconds | None = None
    reply: ClientMilliseconds | None = None
    rendered: ClientMilliseconds | None = None


class CorrelationMiddleware:
    """Give every request a correlation id (reusing a well-formed X-Correlation-ID) and echo it back.

    It also notes when the request arrived, the start of the request's timings.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        incoming = dict(scope["headers"]).get(b"x-correlation-id", b"").decode("latin-1")
        correlation_id = incoming if CORRELATION_ID.fullmatch(incoming) else uuid.uuid4().hex[:16]
        state = scope.setdefault("state", {})
        state["correlation_id"], state["started"] = correlation_id, started

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
    timings = Timings(request.state.correlation_id, request.state.started)
    try:
        with timings.stage("session"):
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
        timings=timings,
    )


def log_request(
    ctx: Context, event: str, outcome: str, error: str | None, timings: dict, assistant: str = "", **numbers: int
) -> None:
    """One line per request. Its extra fields become Application Insights custom dimensions.

    They hold the user id, categories and numbers only, never message text.
    """
    total = timings["ms"].get("total", 0.0)
    log.info(
        "%s %s persona=%s domains=%s outcome=%s total_ms=%.0f",
        "chat" if event == "flow" else event,
        ctx.correlation_id,
        ctx.user.id,
        ",".join(ctx.domains),
        outcome,
        total,
        extra={
            "correlation_id": ctx.correlation_id,
            "user_id": ctx.user.id,
            "event": event,
            **({"assistant": assistant} if assistant else {}),
            "outcome": outcome,
            "error_category": error or "",
            "total_ms": total,
            "stage_ms": json.dumps(timings["ms"]),
            **numbers,
        },
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
    event = "flow" if flow.get("id") else "chat"
    error_category = error.get("category") or (None if reply else "runtime_error")
    timings, usage = ctx.timings.record(), ctx.usage.record()
    ctx.store.audit(
        correlation_id=ctx.correlation_id,
        session_id=ctx.session_id,
        persona=ctx.user.id,
        scenario=ctx.scenario or None,
        event=event,
        flow_id=flow.get("id"),
        flow_version=flow.get("version"),
        tools=[{"tool": call["tool"], "result": call["result"]} for call in ctx.calls],
        inputs=[{"tool": call["tool"], **call["inputs"]} for call in ctx.calls if call["inputs"]],
        domains=list(ctx.domains),
        offered_tools=list(ctx.offered),
        response_category=f"error:{error['category']}" if error else category,
        confirmation="prepared" if ctx.actions else "none",
        outcome=outcome,
        error_category=error_category,
        assistant=ctx.assistant,
        timings=timings,
        answer_id=(reply or {}).get("answer_id"),
        usage=usage,
    )
    log_request(ctx, event, outcome, error_category, timings, ctx.assistant, **usage)


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.validate()
    check_registry()
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
    vocabulary = Vocabulary.build(flows)
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
    commit = settings.app_commit or git_commit()
    stamp = {"app_version": app_version() + (f"+{commit}" if commit else ""), "data_source": services.kind}
    app.state.store = Store(settings.db, stamp)
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

        def frame(kind: str, data: dict) -> str:
            ctx.timings.mark("first_event")
            if kind == "reply":
                ctx.timings.mark("reply")
            return f"event: {kind}\ndata: {json.dumps(data)}\n\n"

        def persist(reply: dict) -> None:
            # Every stored reply gets a stable id: the answer that feedback rates.
            reply["answer_id"] = secrets.token_urlsafe(16)
            with ctx.timings.stage("persist"):
                ctx.store.add_turn(ctx.session_id, body.message, reply, reply["answer_id"], ctx.correlation_id)

        def events() -> Iterator[str]:
            reply = None
            try:
                run = runner.run(body.message, ctx) if use_model else demo_run(body.message, ctx)
                try:
                    for kind, data in run:
                        if kind == "reply":
                            reply = data
                            persist(data)
                        yield frame(kind, data)
                except IntegrationError as exc:
                    reply = error_reply(exc, ctx)
                    persist(reply)
                    yield frame("reply", reply)
            except Exception:
                log.exception("Chat %s failed; no unverified answer released", ctx.correlation_id)
                ctx.store.record_unanswered(ctx.session_id, "runtime_error")
                ctx.timings.mark("first_event")
                yield 'event: error\ndata: {"message":"The run failed. Check the backend log and retry."}\n\n'
            finally:
                ctx.timings.mark("total")
                audit_reply(ctx, reply)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.post("/api/actions/{action_id}/submit")
    def submit(action_id: str, body: Confirmation, ctx: Annotated[Context, Depends(context)]):
        kind, outcome, error = "", "not_submitted", None
        try:
            action = ctx.store.action(ctx.session_id, action_id)
            kind = action["kind"]
            feature = "support" if kind == "ticket" else "reports"
            if feature not in ctx.features:
                error = "disabled"
                raise HTTPException(403, FEATURE_OFF[feature])
            with ctx.timings.stage("tool", "tools", f"tool.submit_{kind}", tool=f"submit_{kind}"):
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
            ctx.timings.mark("total")
            timings = ctx.timings.record()
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
                timings=timings,
            )
            log_request(ctx, "submit", outcome, error, timings)

    @app.get("/api/actions/{action_id}/status")
    def action_status(action_id: str, ctx: Annotated[Context, Depends(context)]):
        """A confirmed report's job state and download link, so the widget can show the link right away."""
        output = ctx.execute("get_report_status", {"action_id": action_id}, caller="user")
        ctx.timings.mark("total")
        timings = ctx.timings.record()
        outcome = ctx.calls[-1]["result"] if ctx.calls else None
        ctx.store.audit(
            correlation_id=ctx.correlation_id,
            session_id=ctx.session_id,
            persona=ctx.user.id,
            scenario=ctx.scenario or None,
            event="status",
            tools=[{"tool": call["tool"], "result": call["result"]} for call in ctx.calls],
            outcome=outcome,
            error_category=output.get("category"),
            timings=timings,
        )
        log_request(ctx, "status", outcome or "", output.get("category"), timings)
        if "error" not in output:
            return output["data"]
        status = {"not_found": 404, "forbidden": 403, "disabled": 403, "invalid_input": 400}.get(
            output["category"], 503
        )
        raise HTTPException(status, output["error"], headers={"X-Error-Category": output["category"]})

    @app.post("/api/timings", status_code=204)
    def client_timing(body: ClientTiming, ctx: Annotated[Context, Depends(context)]) -> None:
        """Record the widget's timings of a chat request from this session, once, numbers only."""
        requests = ctx.store.request_count(ctx.session_id, body.correlation_id)
        if not requests:
            raise HTTPException(404, "Request unavailable.")
        if requests > 1:
            # A reused X-Correlation-ID can't be matched to one request.
            raise HTTPException(409, "Timings can't be matched to a single request.")
        ms = body.model_dump(exclude={"correlation_id"}, exclude_none=True)
        try:
            ctx.store.audit(
                correlation_id=body.correlation_id,
                session_id=ctx.session_id,
                persona=ctx.user.id,
                scenario=ctx.scenario or None,
                event="client_timing",
                timings={"ms": ms, "count": dict.fromkeys(ms, 1)},
            )
        except DuplicateRecord:
            raise HTTPException(409, "Timings already recorded.") from None

    @app.post("/api/answers/{answer_id}/feedback")
    def answer_feedback(answer_id: str, body: Feedback, ctx: Annotated[Context, Depends(context)]):
        """Rate a completed answer of this session. Stores categories and numbers, never question or answer text."""
        error = "unexpected"
        try:
            if not ANSWER_ID.fullmatch(answer_id):
                raise MissingRecord("Answer unavailable.")
            answer = ctx.store.answer(ctx.session_id, answer_id)
            if not rateable(answer["reply"]):
                error = "not_rateable"
                raise HTTPException(409, "Only completed answers can be rated.")
            record = ctx.store.answer_record(ctx.session_id, answer_id)
            if record is None:
                # The request's record is written as its stream closes; until then the context is incomplete.
                error = "pending"
                raise HTTPException(409, "This answer is still being recorded. Try again in a moment.")
            with ctx.timings.stage("persist"):
                saved = ctx.store.save_feedback(
                    answer_id,
                    session_id=ctx.session_id,
                    user_id=ctx.user.id,
                    correlation_id=answer["correlation_id"],
                    rating=body.rating,
                    reason=body.reason,
                    context=answer_context(answer["reply"], record, vocabulary),
                )
            error = ""
            return {"answer_id": answer_id, "rating": saved["rating"], "reason": saved["reason"]}
        except MissingRecord:
            error = "not_found"
            raise HTTPException(404, "Answer unavailable.") from None
        finally:
            ctx.timings.mark("total")
            timings = ctx.timings.record()
            outcome = body.rating if not error else "not_recorded"
            rated = {"answer_id": answer_id, "rating": body.rating, **({"reason": body.reason} if body.reason else {})}
            ctx.store.audit(
                correlation_id=ctx.correlation_id,
                session_id=ctx.session_id,
                persona=ctx.user.id,
                scenario=ctx.scenario or None,
                event="feedback",
                # An id that matched no answer of this session is client input: it is never recorded.
                inputs=[] if error else [rated],
                outcome=outcome,
                error_category=error or None,
                timings=timings,
            )
            log_request(ctx, "feedback", outcome, error or None, timings)

    @app.get("/reference/{path:path}")
    def reference(path: str):
        return {"synthetic": True, "message": "Placeholder view only; no real Alsense report or ticket was sent."}

    @app.get("/system", include_in_schema=False)
    def system_page():
        return FileResponse(ROOT / "static" / "system.html")

    app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="widget")
    return app
