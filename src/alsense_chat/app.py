import hmac
import json
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import ROOT, Settings, load_settings
from .runner import FoundryRunner, demo_run
from .source import AlsenseSource, MockAlsenseSource
from .store import MissingRecord, Store
from .tools import Context, model_definition

log = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona: Literal["alice", "bob", "external"]
    access_code: str = Field(default="", max_length=256)


class Chat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=6000)


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


def context(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> Context:
    state = request.app.state
    if not state.settings.enabled:
        raise HTTPException(403, "Chat is disabled by the backend feature flag.")
    if credentials is None:
        raise HTTPException(401, "A bearer session token is required.")
    try:
        session_id, user = state.store.authenticate(credentials.credentials)
    except MissingRecord as exc:
        raise HTTPException(401, str(exc)) from None
    if not user.internal:
        raise HTTPException(403, "Chat is restricted to internal users.")
    return Context(state.source, state.store, session_id, user)


def create_app(settings: Settings | None = None, source: AlsenseSource | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.validate()
    model_definition()
    source = source or MockAlsenseSource()

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
        yield
        if app.state.runner:
            app.state.runner.close()

    app = FastAPI(title="Alsense synthetic reference starter", lifespan=lifespan)
    app.state.settings = settings
    app.state.source = source
    app.state.store = Store(settings.db)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "mode": settings.agent_mode, "data_source": "synthetic",
                "access_code_required": settings.cloud_demo, "production_ready": False}

    @app.post("/api/demo/session")
    def login(body: Login):
        if not settings.demo_login or not settings.enabled:
            raise HTTPException(403, "Demo login unavailable.")
        if settings.cloud_demo and not hmac.compare_digest(
            body.access_code.encode(), settings.demo_access_code.encode()
        ):
            raise HTTPException(401, "Demo access code invalid.")
        user = source.users_me(body.persona)
        if not user.internal:
            raise HTTPException(403, "Chat is restricted to internal users.")
        token, session_id = app.state.store.create_session(user, settings.ttl_seconds)
        return {"token": token, "session_id": session_id, "user": user.model_dump()}

    @app.get("/api/session")
    def session(ctx: Annotated[Context, Depends(context)]):
        return {"user": ctx.user.model_dump(), "history": ctx.store.history(ctx.session_id)}

    @app.post("/api/chat")
    def chat(body: Chat, request: Request, ctx: Annotated[Context, Depends(context)]):
        def events() -> Iterator[str]:
            try:
                runner = request.app.state.runner
                # Keep the reference form commands deterministic in model mode too.
                run = (
                    runner.run(body.message, ctx)
                    if runner and not body.message.startswith("/")
                    else demo_run(body.message, ctx)
                )
                for kind, data in run:
                    if kind == "reply":
                        ctx.store.add_turn(ctx.session_id, body.message, data)
                    yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"
            except Exception:
                log.exception("Chat failed; no unverified answer released")
                ctx.store.record_unanswered(ctx.session_id, "runtime_error")
                yield 'event: error\ndata: {"message":"The run failed. Check the backend log and retry."}\n\n'
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.post("/api/actions/{action_id}/submit")
    def submit(action_id: str, body: Confirmation, ctx: Annotated[Context, Depends(context)]):
        def execute(action: dict):
            ctx.validate_action(action["kind"], action["payload"])
            return ctx.source.submit(ctx.user, action["kind"], action["payload"], action["id"])
        try:
            action = ctx.store.action(ctx.session_id, action_id)
            ctx.validate_action(action["kind"], action["payload"])
            return ctx.store.submit(ctx.session_id, action_id, execute)
        except MissingRecord:
            raise HTTPException(404, "Action unavailable.") from None
        except ValueError:
            raise HTTPException(403, "Action is no longer permitted.") from None

    @app.get("/reference/{path:path}")
    def reference(path: str):
        return {"synthetic": True, "message": "Placeholder view only; no real Alsense report or ticket was sent."}

    app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="widget")
    return app
