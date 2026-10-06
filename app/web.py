import logging
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, Field

from app.browser_demo_workflow import process_browser_demo_message
from integrations.ai.demo_replay_interpreter import DemoReplayInterpreter
from integrations.ai.openai_customer_request_client import (
    OpenAICustomerRequestClient,
)
from integrations.ai.openai_customer_request_interpreter import (
    OpenAICustomerRequestInterpreter,
)


WEB_DIR = Path(__file__).resolve().parent.parent / "web"
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Carbit Oy — tarjousdemo",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=2000)
    mode: Literal["openai", "replay"]


def get_interpreter(mode: str):
    if mode == "replay":
        return DemoReplayInterpreter()

    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="OpenAI-avain puuttuu palvelimen asetuksista. "
            "Voit käyttää tallennettua demotapausta.",
        )

    client = OpenAICustomerRequestClient()
    client.client = client.client.with_options(
        timeout=20.0,
        max_retries=0,
    )
    return OpenAICustomerRequestInterpreter(client=client)


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "carbit-demo",
    }


@app.post("/api/intake")
def intake(request: IntakeRequest):
    try:
        result = process_browser_demo_message(
            message=request.message,
            interpreter=get_interpreter(request.mode),
        )
    except OpenAIError:
        logger.warning("OpenAI intake request failed")
        raise HTTPException(
            status_code=502,
            detail="OpenAI-tulkinta epäonnistui. Yritä uudelleen "
            "tai valitse tallennettu demotapaus.",
        ) from None
    except ValueError:
        detail = (
            "Tallennettu demotila tukee vain esimerkkiviestejä."
            if request.mode == "replay"
            else "AI-tulkintaa ei voitu käsitellä turvallisesti. "
            "Tarjousta ei muodostettu."
        )
        raise HTTPException(status_code=422, detail=detail) from None

    interpretation = result.intake.interpretation
    reason = None
    if result.proposal is None:
        reason = (
            "Diagnoosin tarve"
            if interpretation.diagnosis_required
            else "Epäselvät tiedot tai demon rajauksen ulkopuolinen työ"
        )

    return {
        "mode": request.mode,
        "message": request.message,
        "interpretation": interpretation.model_dump(mode="json"),
        "human_required_reason": reason,
        "proposal": (
            result.proposal.model_dump(mode="json")
            if result.proposal is not None
            else None
        ),
        "demo_data": True,
    }
