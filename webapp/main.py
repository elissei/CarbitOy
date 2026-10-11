from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation, Overflow
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from domain.models.labour_time import LabourTime, LabourTimeSource
from services.labour_pricing import (
    VAT_RATE,
    calculate_labour_cost,
    split_inclusive_vat,
)
from webapp.database import (
    IdempotencyConflictError,
    VersionConflictError,
    WorkNotFoundError,
    WorkRepository,
)
from webapp.auth_crypto import TotpCipher
from webapp.auth_csrf import CsrfProtector, SESSION_COOKIE
from webapp.auth_repository import AuthRepository
from webapp.auth_service import AuthService, AuthenticationError, PermissionDeniedError
from webapp.auth_settings import AuthSettings
from webapp.http_access import HttpAccessGate, validate_route_coverage
from webapp.http_auth import register_auth_routes
from webapp.business_identity import BusinessActor
from webapp.labour_service import WorkshopPermission
from webapp.workshop_identity import resolve_workshop_context


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = BASE_DIR.parent / "var" / "carbit.db"


class CreateWorkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registration: str = Field(min_length=1, max_length=20)
    customer_description: str = Field(min_length=1, max_length=4000)
    actor: str | None = Field(default=None, min_length=1, max_length=200, deprecated=True)
    idempotency_key: UUID

    @field_validator("registration", "customer_description", "actor")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Kenttä ei voi olla tyhjä")
        return value

    @field_validator("registration")
    @classmethod
    def normalize_registration(cls, value: str) -> str:
        return value.upper()


class RecordLabourRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    hours: Decimal = Field(gt=0)
    work_description: str = Field(min_length=1, max_length=2000)
    recorded_by: str | None = Field(default=None, min_length=1, max_length=200, deprecated=True)
    autodata_reference: str | None = Field(default=None, max_length=500)

    @field_validator("work_description", "recorded_by")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Kenttä ei voi olla tyhjä")
        return value

    @field_validator("autodata_reference")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


def _business_actor(request: Request) -> BusinessActor:
    """After body validation, resolve current writer identity from the real session."""
    try:
        context = resolve_workshop_context(
            request.app.state.auth_service,
            request.state.security_cookies.get(SESSION_COOKIE),
            required_permission=WorkshopPermission.SAVE_LABOUR_PLAN,
        )
        if context.actor is None:
            raise ValueError("missing authenticated attribution")
        return context.actor
    except PermissionDeniedError as error:
        raise HTTPException(status_code=403, detail="Permission denied") from error
    except AuthenticationError as error:
        raise HTTPException(status_code=401, detail="Authentication required") from error
    except (sqlite3.DatabaseError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise HTTPException(status_code=503, detail="Authentication unavailable") from error


def create_app(
    database_path: str | Path | None = None, *, auth_settings: AuthSettings | None = None,
) -> FastAPI:
    selected_path = Path(
        database_path or os.environ.get("CARBIT_DATABASE", DEFAULT_DATABASE)
    )
    repository = WorkRepository(selected_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        settings = auth_settings if auth_settings is not None else AuthSettings.from_environment()
        application.state.route_access = validate_route_coverage(application)
        auth_repository = AuthRepository(settings.database_path)
        auth_repository.initialize()
        application.state.auth_settings = settings
        application.state.auth_service = AuthService(
            auth_repository,
            TotpCipher(tuple(key.get_secret_value().encode("ascii") for key in settings.totp_keys)),
            policy=settings.policy,
        )
        application.state.csrf_protector = CsrfProtector(
            settings, clock=lambda: application.state.auth_service.clock(),
        )
        selected_path.parent.mkdir(parents=True, exist_ok=True)
        repository.initialize()
        yield

    app = FastAPI(
        title="Carbit työjono", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.add_middleware(HttpAccessGate)
    app.state.repository = repository
    register_auth_routes(app)
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    login_headers = {
        "Cache-Control": "no-store", "Pragma": "no-cache", "Expires": "0",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }

    @app.get("/login", include_in_schema=False)
    def login_page() -> FileResponse:
        return FileResponse(BASE_DIR / "browser_auth" / "login.html", media_type="text/html", headers=login_headers)

    @app.get("/login.js", include_in_schema=False)
    def login_script() -> FileResponse:
        return FileResponse(BASE_DIR / "browser_auth" / "login.js", media_type="text/javascript", headers=login_headers)

    @app.get("/login.css", include_in_schema=False)
    def login_style() -> FileResponse:
        return FileResponse(BASE_DIR / "browser_auth" / "login.css", media_type="text/css", headers=login_headers)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(BASE_DIR / "static" / "index.html")

    @app.get("/api/works")
    def list_works(request: Request):
        return request.app.state.repository.list_works()

    @app.post("/api/works", status_code=status.HTTP_201_CREATED)
    def create_work(payload: CreateWorkRequest, request: Request):
        actor = _business_actor(request)
        try:
            return request.app.state.repository.create_work(
                registration=payload.registration,
                customer_description=payload.customer_description,
                actor=actor,
                idempotency_key=str(payload.idempotency_key),
            )
        except IdempotencyConflictError as error:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Samaa lähetysavainta on jo käytetty eri tiedoilla. "
                    "Avaa uusi tiedustelu aloittaaksesi erillisen kirjauksen."
                ),
            ) from error

    @app.get("/api/works/{work_id}")
    def get_work(work_id: str, request: Request):
        try:
            return request.app.state.repository.get_work(work_id)
        except WorkNotFoundError as error:
            raise HTTPException(status_code=404, detail="Työtä ei löytynyt") from error

    @app.put("/api/works/{work_id}/labour")
    def record_labour(work_id: str, payload: RecordLabourRequest, request: Request):
        actor = _business_actor(request)
        try:
            labour_time = LabourTime(
                hours=payload.hours,
                source=LabourTimeSource.MANUAL_AUTODATA,
                work_description=payload.work_description,
                autodata_reference=payload.autodata_reference,
                recorded_by=str(actor.user_id),
            )
            labour_cost = calculate_labour_cost(labour_time)
            net_price, vat_price = split_inclusive_vat(labour_cost.total_price)
        except (ValidationError, InvalidOperation, Overflow) as error:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Työaika: annettu aika ei tuota laskettavissa olevaa "
                    "positiivista senttihintaa. Tarkista Autodatasta kirjattu aika."
                ),
            ) from error
        snapshot = {
            "hours": str(labour_time.hours),
            "source": labour_time.source.value,
            "work_description": labour_time.work_description or "",
            "recorded_by": labour_time.recorded_by or "",
            "autodata_reference": labour_time.autodata_reference or "",
            "hourly_rate": str(labour_cost.hourly_rate),
            "total_price": str(labour_cost.total_price),
            "net_price": str(net_price),
            "vat_price": str(vat_price),
            "vat_rate": str(VAT_RATE),
            "currency": labour_cost.currency,
        }
        try:
            return request.app.state.repository.record_labour(
                work_id=work_id,
                expected_version=payload.version,
                labour=snapshot,
                actor=actor,
            )
        except WorkNotFoundError as error:
            raise HTTPException(status_code=404, detail="Työtä ei löytynyt") from error
        except VersionConflictError as error:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Työtä on muutettu toisessa selainikkunassa. "
                    "Lataa uusin versio ennen tallennusta."
                ),
            ) from error

    return app


app = create_app()
