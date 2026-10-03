"""HTTP layer: alias resolution, request limits, endpoints."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import engine
from .config import (ALIASES, MAX_MIGRATIONS_PER_REQUEST,
                     MAX_REQUEST_BYTES)

app = FastAPI(title="SQLite Migration Service")


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    length = request.headers.get("content-length")
    if length is not None and int(length) > MAX_REQUEST_BYTES:
        return JSONResponse({"detail": "request too large"}, status_code=413)
    return await call_next(request)


class MigrationItemIn(BaseModel):
    version: int = Field(gt=0)
    description: str
    sql: str


class MigrateRequest(BaseModel):
    expected_version: int = Field(gt=0)
    migrations: list[MigrationItemIn]


@app.exception_handler(engine.MigrationError)
async def migration_error_handler(_req, exc: engine.MigrationError):
    body = {"ok": False, "error": exc.message}
    if exc.version is not None:
        body["failed_version"] = exc.version
    return JSONResponse(body, status_code=exc.status_code)


def _db_path(alias: str):
    path = ALIASES.get(alias)
    if path is None:
        raise HTTPException(status_code=404, detail=f"unknown alias: {alias}")
    return path


@app.get("/aliases")
def list_aliases():
    return {"aliases": sorted(ALIASES)}


@app.get("/db/{alias}/version")
def get_version(alias: str):
    return engine.get_status(_db_path(alias))


@app.post("/db/{alias}/migrate")
def migrate(alias: str, payload: MigrateRequest):
    if len(payload.migrations) > MAX_MIGRATIONS_PER_REQUEST:
        raise HTTPException(status_code=413, detail="too many migrations")
    items = [engine.ManifestItem(m.version, m.description, m.sql)
             for m in payload.migrations]
    result = engine.apply_migrations(
        _db_path(alias), items, payload.expected_version)
    return {"ok": True, **result}
