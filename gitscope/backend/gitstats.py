"""
Git repository analysis. Everything here shells out to the `git` binary and
returns plain dicts that the API layer serialises to JSON.

Two kinds of data are produced:

* `analyze_repo`  – one pass over `git log --numstat`. Returns every commit
  (author, co-authors, timestamp, +/- lines, files touched) plus a few
  server-side aggregates that would be too big to ship per commit
  (lines per file extension per author, most changed files).
  The frontend does the rest (merging identities, excluding people,
  bucketing by week/month) so those operations are instant.

* `OwnershipJob`  – runs `git blame` over every text file at HEAD to find
  out who "owns" the lines that exist *today* (as opposed to churn).
  This can take a while on big repos, so it runs in a background thread,
  reports progress and caches the result on disk keyed by HEAD sha.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

REPOS_ROOT = Path(os.environ.get("REPOS_ROOT", "/repos")).resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
CACHE_DIR = DATA_DIR / "cache"
BLAME_WORKERS = int(os.environ.get("BLAME_WORKERS", "0")) or max(2, min(8, (os.cpu_count() or 4)))
BLAME_MAX_LINES = int(os.environ.get("BLAME_MAX_LINES", "20000"))  # skip monster files (minified bundles etc.)

RS = "\x1e"  # record separator: one per commit
FS = "\x1f"  # field separator inside a record
LOG_FORMAT = f"{RS}%H{FS}%an{FS}%ae{FS}%at{FS}%aI{FS}%P{FS}%s{FS}%b{FS}"

COAUTHOR_RE = re.compile(r"^\s*Co-authored-by:\s*(.*?)\s*<([^>]*)>\s*$", re.IGNORECASE | re.MULTILINE)
RENAME_RE = re.compile(r"^(.*)\{(.*) => (.*)\}(.*)$")

KNOWN_EXTLESS = {"dockerfile", "makefile", "license", "readme", "jenkinsfile", "vagrantfile", "procfile", "gemfile", "rakefile"}


class GitError(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def run_git(repo: Path, *args: str, check: bool = True, stdin: str | None = None) -> str:
    """Run a git command inside `repo` and return stdout as text."""
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=stdin,
    )
    if check and proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout).strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def resolve_repo(rel_path: str) -> Path:
    """Map a user supplied path (relative to REPOS_ROOT, or absolute inside it) to a real directory."""
    raw = (rel_path or "").strip().replace("\\", "/").rstrip("/")
    # A path pasted from the host (e.g. C:/Users/me/code/app) is mapped onto the mount.
    host_root = os.environ.get("REPOS_ROOT_HOST", "").replace("\\", "/").rstrip("/")
    if host_root and raw.lower().startswith(host_root.lower()):
        raw = raw[len(host_root):]
    stripped = raw.lstrip("/")
    candidates = [REPOS_ROOT / stripped if stripped else REPOS_ROOT]
    if raw.startswith("/"):
        candidates.append(Path(raw))  # absolute path inside the container, e.g. /repos/app
    last_error = f"Path must be inside the mounted repos folder ({REPOS_ROOT})."
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            last_error = f"Cannot resolve path: {exc}"
            continue
        if resolved != REPOS_ROOT and REPOS_ROOT not in resolved.parents:
            if resolved.exists():
                last_error = f"{raw} is outside the mounted repos folder. Only paths under {REPOS_ROOT} (your REPOS_ROOT) can be analysed."
            continue
        if not resolved.is_dir():
            last_error = f"Not a directory: {raw or '/'}"
            continue
        return resolved
    raise GitError(last_error)


def display_path(p: Path) -> str:
    """Path relative to REPOS_ROOT, the way users see it in the UI."""
    try:
        rel = p.resolve().relative_to(REPOS_ROOT)
    except ValueError:
        return str(p)
    return "/" + rel.as_posix() if str(rel) != "." else "/"


def is_git_repo(p: Path) -> bool:
    dot = p / ".git"
    return dot.is_dir() or dot.is_file()  # file = worktree / submodule pointer


def resolve_rename(path: str) -> str:
    """Turn numstat rename notation into the new path.
    'src/{old => new}/file.py' -> 'src/new/file.py', 'a.py => b.py' -> 'b.py'."""
    if " => " not in path:
        return path
    m = RENAME_RE.match(path)
    if m:
        return (m.group(1) + m.group(3) + m.group(4)).replace("//", "/")
    return path.split(" => ")[-1]


def ext_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    lower = name.lower()
    if lower in KNOWN_EXTLESS:
        return name
    if lower.startswith("dockerfile"):
        return "Dockerfile"
    if name.startswith(".") and name.count(".") == 1:
        return name  # .gitignore, .env
    if "." in name:
        ext = name.rsplit(".", 1)[-1].lower()
        return ext or "(none)"
    return "(none)"


def parse_ignore(ignore: str | None) -> list[str]:
    if not ignore:
        return []
    parts = re.split(r"[,\n;]+", ignore)
    return [p.strip() for p in parts if p.strip()]


def is_ignored(path: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    base = path.rsplit("/", 1)[-1]
    for pat in patterns:
        pat = pat.replace("\\", "/").lstrip("/")
        if pat.endswith("/"):
            prefix = pat.rstrip("/")
            if path == prefix or path.startswith(prefix + "/") or f"/{prefix}/" in f"/{path}":
                return True
            continue
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(base, pat):
            return True
        if "/" not in pat and fnmatch.fnmatch(path, f"*/{pat}"):
            return True
        if "/" in pat and fnmatch.fnmatch(path, f"*/{pat}"):
            return True
    return False


def parse_offset_minutes(iso: str) -> int:
    """'2026-02-11T14:00:00+01:00' -> 60"""
    m = re.search(r"([+-])(\d\d):?(\d\d)$", iso.strip())
    if not m:
        return 0
    sign = 1 if m.group(1) == "+" else -1
    return sign * (int(m.group(2)) * 60 + int(m.group(3)))


# --------------------------------------------------------------------------- author registry

class AuthorRegistry:
    """Assigns a stable index to each identity. Identity = lower-cased email
    (falls back to name when the email is empty). Different spellings of a
    name that share one email collapse automatically; different emails do
    not, that is what merging in the UI is for."""

    def __init__(self) -> None:
        self.index: dict[str, int] = {}
        self.authors: list[dict[str, Any]] = []

    @staticmethod
    def key_for(name: str, email: str) -> str:
        email = (email or "").strip().lower()
        return email if email else f"name:{(name or '').strip().lower()}"

    def get(self, name: str, email: str) -> int:
        key = self.key_for(name, email)
        idx = self.index.get(key)
        if idx is None:
            idx = len(self.authors)
            self.index[key] = idx
            self.authors.append({"key": key, "name": name.strip() or email, "email": email.strip(), "names": {}})
        names = self.authors[idx]["names"]
        n = name.strip()
        if n:
            names[n] = names.get(n, 0) + 1
        return idx

    def finalize(self) -> list[dict[str, Any]]:
        out = []
        for a in self.authors:
            names = a["names"]
            best = max(names.items(), key=lambda kv: kv[1])[0] if names else a["name"]
            out.append({
                "key": a["key"],
                "name": best,
                "email": a["email"],
                "aliases": sorted(n for n in names if n != best),
            })
        return out


# --------------------------------------------------------------------------- repo info

def repo_info(repo: Path) -> dict[str, Any]:
    if not is_git_repo(repo):
        raise GitError(f"{display_path(repo)} is not a git repository (no .git inside).")
    try:
        head = run_git(repo, "rev-parse", "HEAD").strip()
    except GitError as exc:
        raise GitError("Repository has no commits yet." if "ambiguous argument" in str(exc) or "unknown revision" in str(exc) else str(exc)) from exc
    branch = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    remotes = [line.split()[1] for line in run_git(repo, "remote", "-v", check=False).splitlines() if "(fetch)" in line]
    branches = [b.strip().lstrip("* ").strip() for b in run_git(repo, "branch", "--format=%(refname:short)", check=False).splitlines() if b.strip()]
    count_head = int(run_git(repo, "rev-list", "--count", "HEAD").strip() or 0)
    count_all = int(run_git(repo, "rev-list", "--count", "--all").strip() or 0)
    return {
        "path": display_path(repo),
        "name": repo.name,
        "head": head,
        "branch": branch,
        "branches": branches,
        "remotes": remotes,
        "commits_head": count_head,
        "commits_all": count_all,
    }


# --------------------------------------------------------------------------- git log analysis

_log_cache: dict[str, dict[str, Any]] = {}
_log_lock = threading.Lock()


def analyze_repo(repo: Path, all_branches: bool = False, ignore: str | None = None) -> dict[str, Any]:
    info = repo_info(repo)
    patterns = parse_ignore(ignore)
    cache_key = json.dumps([str(repo), info["head"], all_branches, patterns])
    with _log_lock:
        cached = _log_cache.get(cache_key)
    if cached:
        return cached

    args = ["log", "--numstat", "--no-color", "--encoding=UTF-8", f"--pretty=format:{LOG_FORMAT}"]
    if all_branches:
        args.insert(1, "--all")
    raw = run_git(repo, *args)

    registry = AuthorRegistry()
    commits: list[dict[str, Any]] = []
    ext_by_author: dict[int, dict[str, list[int]]] = {}
    ext_totals: dict[str, list[int]] = {}
    files: dict[str, list[int]] = {}  # path -> [add, del, commits]
    ignored_files: set[str] = set()

    for record in raw.split(RS):
        if not record.strip():
            continue
        parts = record.split(FS)
        if len(parts) < 9:
            continue
        sha, name, email, at, aiso, parents, subject, body, numstat = parts[:9]
        author_idx = registry.get(name, email)
        coauthors: list[int] = []
        for cname, cemail in COAUTHOR_RE.findall(body):
            ci = registry.get(cname, cemail)
            if ci != author_idx and ci not in coauthors:
                coauthors.append(ci)

        add = dele = nfiles = 0
        author_ext = ext_by_author.setdefault(author_idx, {})
        for line in numstat.splitlines():
            if "\t" not in line:
                continue
            a, d, path = line.split("\t", 2)
            path = resolve_rename(path)
            if is_ignored(path, patterns):
                ignored_files.add(path)
                continue
            nfiles += 1
            if a == "-" or d == "-":
                ext = ext_of(path)
                et = ext_totals.setdefault(ext, [0, 0, 0])
                et[2] += 1
                f = files.setdefault(path, [0, 0, 0])
                f[2] += 1
                continue
            ai, di = int(a), int(d)
            add += ai
            dele += di
            ext = ext_of(path)
            ae = author_ext.setdefault(ext, [0, 0])
            ae[0] += ai
            ae[1] += di
            et = ext_totals.setdefault(ext, [0, 0, 0])
            et[0] += ai
            et[1] += di
            et[2] += 1
            f = files.setdefault(path, [0, 0, 0])
            f[0] += ai
            f[1] += di
            f[2] += 1

        commits.append({
            "h": sha[:10],
            "a": author_idx,
            "c": coauthors,
            "t": int(at),
            "o": parse_offset_minutes(aiso),
            "m": 1 if len(parents.split()) > 1 else 0,
            "s": subject,
            "add": add,
            "del": dele,
            "f": nfiles,
        })

    commits.sort(key=lambda c: c["t"])
    top_files = sorted(files.items(), key=lambda kv: kv[1][0] + kv[1][1], reverse=True)[:40]

    result = {
        "repo": info,
        "options": {"all_branches": all_branches, "ignore": patterns},
        "authors": registry.finalize(),
        "commits": commits,
        "ext_by_author": {str(k): v for k, v in ext_by_author.items()},
        "ext_totals": ext_totals,
        "top_files": [{"path": p, "add": v[0], "del": v[1], "commits": v[2]} for p, v in top_files],
        "ignored_files": len(ignored_files),
        "total_files_touched": len(files),
    }
    with _log_lock:
        if len(_log_cache) > 12:  # tiny LRU-ish guard, it is a local tool
            _log_cache.pop(next(iter(_log_cache)))
        _log_cache[cache_key] = result
    return result


# --------------------------------------------------------------------------- ownership (blame)

class OwnershipJob:
    def __init__(self, repo: Path, head: str, patterns: list[str]) -> None:
        self.repo = repo
        self.head = head
        self.patterns = patterns
        self.status = "queued"
        self.done = 0
        self.total = 0
        self.error: str | None = None
        self.result: dict[str, Any] | None = None
        self.thread: threading.Thread | None = None

    @property
    def cache_key(self) -> str:
        return hashlib.sha1(json.dumps([str(self.repo), self.head, self.patterns]).encode()).hexdigest()

    @property
    def cache_file(self) -> Path:
        return CACHE_DIR / f"ownership-{self.cache_key}.json"

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "error": self.error,
            "result": self.result if self.status == "done" else None,
        }

    def start(self) -> None:
        if self.cache_file.exists():
            try:
                self.result = json.loads(self.cache_file.read_text(encoding="utf-8"))
                self.status = "done"
                return
            except (OSError, ValueError):
                pass
        self.status = "running"
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _list_text_files(self) -> list[tuple[str, int]]:
        empty_tree = run_git(self.repo, "hash-object", "-t", "tree", "--stdin", stdin="").strip()
        raw = run_git(self.repo, "diff", "--numstat", empty_tree, self.head)
        files: list[tuple[str, int]] = []
        for line in raw.splitlines():
            if "\t" not in line:
                continue
            a, _d, path = line.split("\t", 2)
            if a == "-":
                continue  # binary
            lines = int(a)
            if lines == 0 or lines > BLAME_MAX_LINES:
                continue
            if is_ignored(path, self.patterns):
                continue
            files.append((path, lines))
        return files

    def _blame_file(self, path: str) -> dict[str, int]:
        out = run_git(self.repo, "blame", "--line-porcelain", "-w", self.head, "--", path, check=False)
        counts: dict[str, int] = {}
        name = ""
        for line in out.splitlines():
            if line.startswith("author "):
                name = line[7:]
            elif line.startswith("author-mail "):
                email = line[12:].strip("<>")
                key = AuthorRegistry.key_for(name, email)
                counts[key] = counts.get(key, 0) + 1
        return counts

    def _run(self) -> None:
        try:
            files = self._list_text_files()
            self.total = len(files)
            by_author: dict[str, dict[str, int]] = {}
            names: dict[str, str] = {}
            file_lines = 0
            with ThreadPoolExecutor(max_workers=BLAME_WORKERS) as pool:
                futures = {pool.submit(self._blame_file, p): p for p, _ in files}
                for fut in as_completed(futures):
                    path = futures[fut]
                    ext = ext_of(path)
                    try:
                        counts = fut.result()
                    except Exception:  # noqa: BLE001 - one bad file must not kill the job
                        counts = {}
                    for key, n in counts.items():
                        bucket = by_author.setdefault(key, {})
                        bucket[ext] = bucket.get(ext, 0) + n
                        file_lines += n
                    self.done += 1
            # names for keys that might not exist in the log (e.g. --all was off)
            self.result = {
                "head": self.head,
                "files": self.total,
                "lines": file_lines,
                "by_author": by_author,
            }
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(self.result), encoding="utf-8")
            self.status = "done"
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.status = "error"


_jobs: dict[str, OwnershipJob] = {}
_jobs_lock = threading.Lock()


def get_or_start_ownership(repo: Path, ignore: str | None, start: bool) -> dict[str, Any]:
    info = repo_info(repo)
    job = OwnershipJob(repo, info["head"], parse_ignore(ignore))
    with _jobs_lock:
        existing = _jobs.get(job.cache_key)
        if existing is None:
            if not start:
                if job.cache_file.exists():
                    job.start()  # loads from disk synchronously
                    _jobs[job.cache_key] = job
                    return job.snapshot()
                return {"status": "idle", "done": 0, "total": 0, "error": None, "result": None}
            _jobs[job.cache_key] = job
            existing = job
            existing.start()
        elif start and existing.status == "error":
            _jobs[job.cache_key] = job
            existing = job
            existing.start()
    return existing.snapshot()


# --------------------------------------------------------------------------- browsing

def browse(rel_path: str) -> dict[str, Any]:
    target = resolve_repo(rel_path) if rel_path else REPOS_ROOT
    entries = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.name.startswith(".") and entry.name != ".git":
                    continue
                if entry.name == ".git":
                    continue
                p = Path(entry.path)
                entries.append({"name": entry.name, "path": display_path(p), "is_repo": is_git_repo(p)})
    except PermissionError as exc:
        raise GitError(f"No permission to read {display_path(target)}") from exc
    entries.sort(key=lambda e: (not e["is_repo"], e["name"].lower()))
    parent = display_path(target.parent) if target != REPOS_ROOT else None
    return {
        "path": display_path(target),
        "parent": parent,
        "is_repo": is_git_repo(target),
        "entries": entries[:500],
        "truncated": len(entries) > 500,
    }


# --------------------------------------------------------------------------- small json stores (identities, recent repos)

_store_lock = threading.Lock()


def _store_path(name: str) -> Path:
    return DATA_DIR / f"{name}.json"


def load_store(name: str) -> dict[str, Any]:
    p = _store_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_store(name: str, data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _store_path(name).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_store_path(name))


def get_identities(repo_path: str) -> dict[str, Any]:
    with _store_lock:
        return load_store("identities").get(repo_path, {"merges": {}, "excluded": [], "names": {}})


def set_identities(repo_path: str, data: dict[str, Any]) -> None:
    clean = {
        "merges": {str(k): str(v) for k, v in (data.get("merges") or {}).items()},
        "excluded": [str(x) for x in (data.get("excluded") or [])],
        "names": {str(k): str(v) for k, v in (data.get("names") or {}).items()},
    }
    with _store_lock:
        store = load_store("identities")
        store[repo_path] = clean
        save_store("identities", store)


def touch_recent(repo_path: str, name: str) -> None:
    import time
    with _store_lock:
        store = load_store("recent")
        items = [r for r in store.get("items", []) if r.get("path") != repo_path]
        items.insert(0, {"path": repo_path, "name": name, "at": int(time.time())})
        store["items"] = items[:12]
        save_store("recent", store)


def get_recent() -> list[dict[str, Any]]:
    with _store_lock:
        return load_store("recent").get("items", [])
