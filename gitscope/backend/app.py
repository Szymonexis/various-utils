"""
HTTP layer. Thin on purpose: validate inputs, call gitstats, return JSON.
Static frontend is served from ../frontend.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import gitstats
from gitstats import GitError

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
HOST_ROOT = os.environ.get("REPOS_ROOT_HOST", "")  # purely cosmetic: what the folder is called on the host

app = FastAPI(title="gitscope", docs_url="/api/docs", redoc_url=None)


def _fail(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


@app.get("/api/config")
def config() -> dict[str, Any]:
    return {
        "repos_root": str(gitstats.REPOS_ROOT),
        "host_root": HOST_ROOT,
        "root_mounted": gitstats.REPOS_ROOT.exists(),
        "blame_workers": gitstats.BLAME_WORKERS,
        "blame_max_lines": gitstats.BLAME_MAX_LINES,
    }


@app.get("/api/recent")
def recent() -> list[dict[str, Any]]:
    return gitstats.get_recent()


@app.get("/api/browse")
def browse(path: str = Query("")) -> dict[str, Any]:
    try:
        return gitstats.browse(path)
    except GitError as exc:
        raise _fail(exc)


@app.get("/api/repo")
def repo(path: str = Query(...)) -> dict[str, Any]:
    try:
        return gitstats.repo_info(gitstats.resolve_repo(path))
    except GitError as exc:
        raise _fail(exc)


@app.get("/api/analyze")
def analyze(path: str = Query(...), all_branches: bool = Query(False, alias="all"), ignore: str = Query("")) -> Any:
    try:
        repo_dir = gitstats.resolve_repo(path)
        data = gitstats.analyze_repo(repo_dir, all_branches=all_branches, ignore=ignore)
    except GitError as exc:
        raise _fail(exc)
    gitstats.touch_recent(data["repo"]["path"], data["repo"]["name"])
    return JSONResponse(data)


@app.get("/api/ownership")
def ownership_status(path: str = Query(...), ignore: str = Query("")) -> dict[str, Any]:
    try:
        return gitstats.get_or_start_ownership(gitstats.resolve_repo(path), ignore, start=False)
    except GitError as exc:
        raise _fail(exc)


@app.post("/api/ownership")
def ownership_start(path: str = Query(...), ignore: str = Query("")) -> dict[str, Any]:
    try:
        return gitstats.get_or_start_ownership(gitstats.resolve_repo(path), ignore, start=True)
    except GitError as exc:
        raise _fail(exc)


class Identities(BaseModel):
    merges: dict[str, str] = {}
    excluded: list[str] = []
    names: dict[str, str] = {}


@app.get("/api/identities")
def identities_get(path: str = Query(...)) -> dict[str, Any]:
    try:
        repo_dir = gitstats.resolve_repo(path)
    except GitError as exc:
        raise _fail(exc)
    return gitstats.get_identities(gitstats.display_path(repo_dir))


@app.put("/api/identities")
def identities_put(body: Identities, path: str = Query(...)) -> dict[str, str]:
    try:
        repo_dir = gitstats.resolve_repo(path)
    except GitError as exc:
        raise _fail(exc)
    gitstats.set_identities(gitstats.display_path(repo_dir), body.model_dump())
    return {"ok": "saved"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
