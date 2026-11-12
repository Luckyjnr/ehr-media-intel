"""FastAPI app: semantic search and patient lookup.

Run it with:
    uvicorn app.api.main:app --reload

Interactive docs are at http://localhost:8000/docs once it is running.
"""

import datetime as dt
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from app.fhir.store import DEFAULT_DB_PATH, get_bundle
from app.search.index import CHROMA_PATH, PROJECT_ROOT, Embedder, current_summary, load_embedder, open_collection
from app.search.searcher import search
from app.summaries.models import ClinicalSummary
from app.summaries.prompt import extract_facts

ResourceType = Literal["DocumentReference", "DiagnosticReport", "Summary"]

# The clinician search page (Task 5): plain HTML + JS, no build step.
FRONTEND_DIR = PROJECT_ROOT / "frontend"

# ---------------------------------------------------------------------------
# Request and response shapes (FastAPI validates and documents these)
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be empty")
        return value


class SearchResult(BaseModel):
    score: float  # 1 - cosine distance; higher is a closer match
    mrn: str
    patient_name: str
    resource_type: ResourceType
    resource_id: str
    date: str | None
    snippet: str
    summary_snippet: str | None  # start of the patient's AI summary, for result cards


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    took_ms: float


class ResourceItem(BaseModel):
    type: str
    id: str
    date: str | None
    description: str


class PatientResponse(BaseModel):
    mrn: str
    patient_name: str
    summary: ClinicalSummary | None  # None if no summary has been generated yet
    resources: list[ResourceItem]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict:
    return {"status": "ok", "indexed_items": request.app.state.collection.count()}


# Plain "def" (not "async def"): embedding and Chroma are blocking work,
# and FastAPI runs plain functions in a thread pool so the server stays responsive.
@router.post("/search", response_model=SearchResponse)
def search_records(
    body: SearchRequest,
    request: Request,
    resource_type: ResourceType | None = Query(None, description="Only return this kind of item"),
    date_from: dt.date | None = Query(None, description="YYYY-MM-DD, inclusive"),
    date_to: dt.date | None = Query(None, description="YYYY-MM-DD, inclusive"),
) -> SearchResponse:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    started = time.perf_counter()
    results = search(
        body.query,
        request.app.state.embed,
        request.app.state.collection,
        resource_type=resource_type,
        date_from=date_from,
        date_to=date_to,
        top_k=5,
    )
    took_ms = round((time.perf_counter() - started) * 1000, 1)
    return SearchResponse(query=body.query, results=results, took_ms=took_ms)


def describe_resources(bundle: dict) -> list[ResourceItem]:
    """A short line for each resource in the bundle."""
    items = []
    for entry in bundle["entry"]:
        resource = entry["resource"]
        resource_type = resource["resourceType"]
        date = None

        if resource_type == "Patient":
            name = resource.get("name", [{}])[0]
            description = " ".join(name.get("given", []) + [name.get("family", "")]).strip() or "Patient"
            date = resource.get("birthDate")  # the only date a Patient has
        elif resource_type == "Encounter":
            identifiers = resource.get("identifier", [])
            description = f"Encounter {identifiers[0]['value']}" if identifiers else "Encounter"
        elif resource_type == "DocumentReference":
            description = resource.get("description") or resource.get("type", {}).get("text", "Document")
            date = resource.get("context", {}).get("period", {}).get("start")
        elif resource_type == "DiagnosticReport":
            description = resource.get("conclusion") or resource.get("code", {}).get("text", "Lab report")
            date = resource.get("effectiveDateTime")
        else:
            description = resource_type

        items.append(ResourceItem(type=resource_type, id=resource["id"], date=date, description=description))
    return items


@router.get("/patients/{mrn}", response_model=PatientResponse)
def get_patient(mrn: str, request: Request) -> PatientResponse:
    db_path = request.app.state.db_path
    bundle = get_bundle(mrn, db_path)
    if bundle is None:
        raise HTTPException(
            status_code=404,
            detail=f"No patient record for MRN {mrn}. It may not exist, or it may be held for review.",
        )
    return PatientResponse(
        mrn=mrn,
        patient_name=extract_facts(bundle)["patient"]["name"],
        summary=current_summary(mrn, bundle, db_path),
        resources=describe_resources(bundle),
    )


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------


def create_app(
    embedder: Embedder | None = None,
    chroma_path: Path = CHROMA_PATH,
    db_path: Path = DEFAULT_DB_PATH,
) -> FastAPI:
    """Build the app. Tests pass a fake embedder and temporary paths."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Runs once when the server starts, not on every request.
        # Loading the model takes seconds; a search then takes milliseconds.
        load_dotenv(PROJECT_ROOT / ".env")  # for ANTHROPIC_MODEL (which summaries to show)
        app.state.embed = embedder or load_embedder()
        app.state.collection = open_collection(chroma_path)
        # One throwaway search. The first embedding and the first Chroma query
        # are slow (the index is read from disk), so we pay that cost here at
        # startup instead of in the first user's search.
        warm_up_vector = app.state.embed(["warm up"])
        if app.state.collection.count() > 0:
            app.state.collection.query(query_embeddings=warm_up_vector, n_results=1)
        app.state.db_path = db_path
        yield

    app = FastAPI(title="EHR Media Intelligence API", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        # Any port on localhost, so the Task 5 page can call the API in development.
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.include_router(router)

    # The search page at "/", and its script at "/static/app.js".
    # Mounted under /static (not "/") so it can never shadow an API route.
    @app.get("/", include_in_schema=False)
    def index_page() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
    return app


# The app uvicorn runs. Nothing heavy happens here: the model loads at startup.
app = create_app()
