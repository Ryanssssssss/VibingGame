"""FastAPI web server for GodotVibe interactive UI."""

from __future__ import annotations

import os
import sys
import json
import logging
import time
import uuid
import asyncio
import threading
import queue
import signal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

# Ensure UTF-8
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vibe_tools.api_parser import APIKnowledgeBase
from vibe_tools.models import ASSET_EXTENSIONS_ALL, PROJECT_SOURCE_EXTENSIONS, is_ignored_path, classify_asset
from vibe_tools.project_generator import ProjectGenerator
from vibe_tools.godot_runner import GodotRunner
from vibe_tools.agent import GameGenerator
from vibe_tools.templates import TEMPLATES, get_template

logger = logging.getLogger(__name__)

# Paths
ENGINE_ROOT = Path(__file__).resolve().parent.parent
GODOT_EXE = ENGINE_ROOT / "bin" / "godot.windows.editor.x86_64.exe"
DOC_CLASSES_DIR = ENGINE_ROOT / "doc" / "classes"
API_CACHE_DIR = Path(__file__).resolve().parent / "api_cache"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_OUTPUT_DIR = Path.home() / "GodotVibeProjects"
CONVERSATIONS_DIR = Path(__file__).resolve().parent / "conversations"

app = FastAPI(title="GodotVibe", version="0.2.0")

_ALLOWED_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:8899,http://127.0.0.1:8899").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# Serve static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─── Session Management (multi-user isolation) ───

class UserSession:
    """Per-user isolated state."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.created = time.time()
        self.last_active = time.time()
        self._generator: GameGenerator | None = None
        self._generator_lock = threading.Lock()
        self._last_config: dict[str, str | None] = {"api_key": None, "base_url": None, "model": None}
        self._pids: set[int] = set()  # tracked Godot processes
        self._pids_lock = threading.Lock()
        # Each session gets its own subdirectory
        self.output_dir = DEFAULT_OUTPUT_DIR / session_id
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def touch(self):
        self.last_active = time.time()

    def get_generator(self, api_key: str | None = None, base_url: str | None = None, **_kwargs) -> GameGenerator:
        """Get or create GameGenerator. The agent model is ALWAYS claude-4.6-opus (hardcoded).
        Only api_key and base_url are configurable; the `model` kwarg is ignored."""
        effective_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY")
        effective_url = base_url or os.getenv("LLM_BASE_URL") or os.getenv("GEMINI_BASE_URL")

        with self._generator_lock:
            config_changed = (
                self._generator is None
                or effective_key != self._last_config["api_key"]
                or effective_url != self._last_config["base_url"]
            )
            if config_changed:
                self._generator = GameGenerator(
                    api_key=effective_key,
                    base_url=effective_url,
                )
                self._last_config = {"api_key": effective_key, "base_url": effective_url}
            return self._generator

    def track_pid(self, pid: int):
        with self._pids_lock:
            self._pids.add(pid)

    def kill_all_processes(self):
        with self._pids_lock:
            for pid in list(self._pids):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            self._pids.clear()

    def owns_project(self, project_dir: str, local_mode: bool = False) -> bool:
        """Check if a project directory belongs to this session.
        
        In local_mode, any project under DEFAULT_OUTPUT_DIR is accepted
        (single-user local development doesn't need strict session isolation).
        """
        try:
            resolved = Path(project_dir).resolve()
            # Local mode: accept any project under the global output dir
            if local_mode:
                resolved.relative_to(DEFAULT_OUTPUT_DIR.resolve())
                return True
            # Remote mode: strict session isolation
            resolved.relative_to(self.output_dir.resolve())
            return True
        except ValueError:
            return False


class SessionManager:
    """Thread-safe session store."""

    SESSION_COOKIE = "godotvibe_session"
    SESSION_TTL = 86400 * 7  # 7 days

    def __init__(self):
        self._sessions: dict[str, UserSession] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str | None) -> tuple[UserSession, bool]:
        """Return (session, is_new). Creates a new session if id is None or unknown."""
        with self._lock:
            if session_id and session_id in self._sessions:
                sess = self._sessions[session_id]
                sess.touch()
                return sess, False
            new_id = str(uuid.uuid4())
            sess = UserSession(new_id)
            self._sessions[new_id] = sess
            return sess, True

    def get(self, session_id: str) -> UserSession | None:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess:
                sess.touch()
            return sess

    def cleanup_expired(self):
        now = time.time()
        with self._lock:
            expired = [sid for sid, s in self._sessions.items() if now - s.last_active > self.SESSION_TTL]
            for sid in expired:
                self._sessions[sid].kill_all_processes()
                del self._sessions[sid]


_session_mgr = SessionManager()


def _get_session(request: Request) -> UserSession:
    """Extract session from request (set by middleware)."""
    return request.state.session


class SessionMiddleware(BaseHTTPMiddleware):
    """Attach a UserSession to every request via cookie."""

    async def dispatch(self, request: Request, call_next):
        session_id = request.cookies.get(SessionManager.SESSION_COOKIE)
        sess, is_new = _session_mgr.get_or_create(session_id)
        request.state.session = sess

        response = await call_next(request)

        if is_new:
            response.set_cookie(
                key=SessionManager.SESSION_COOKIE,
                value=sess.session_id,
                max_age=SessionManager.SESSION_TTL,
                httponly=True,
                samesite="lax",
            )
        return response


app.add_middleware(SessionMiddleware)


# ─── Global state (shared, read-only or stateless) ───

_api_kb: APIKnowledgeBase | None = None
_runner: GodotRunner | None = None
_project_gen = ProjectGenerator()


def _resolve_safe_path(base_dir: str | Path, rel_path: str) -> Path | None:
    """Resolve *rel_path* inside *base_dir*, blocking path traversal."""
    base = Path(base_dir).resolve()
    target = (base / rel_path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def _get_api_kb() -> APIKnowledgeBase:
    global _api_kb
    if _api_kb is None:
        cache_path = API_CACHE_DIR / "api_knowledge.json"
        if cache_path.exists():
            _api_kb = APIKnowledgeBase.load_from_cache(str(cache_path))
        else:
            _api_kb = APIKnowledgeBase.build_from_xml(str(DOC_CLASSES_DIR))
            _api_kb.save_to_cache(str(cache_path))
    return _api_kb


def _get_runner() -> GodotRunner | None:
    global _runner
    if _runner is None:
        try:
            _runner = GodotRunner(str(GODOT_EXE) if GODOT_EXE.exists() else None)
        except FileNotFoundError:
            return None
    return _runner


# ─── Pydantic Models ───

class ChatRequest(BaseModel):
    message: str
    output_dir: str | None = None
    project_dir: str | None = None
    api_key: str | None = None
    base_url: str | None = None


class GenerateRequest(BaseModel):
    prompt: str
    output_dir: str | None = None
    project_dir: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    images: list[str] | None = None


class TemplateRequest(BaseModel):
    template_name: str
    project_name: str
    output_dir: str | None = None


class ConfigRequest(BaseModel):
    api_key: str
    base_url: str | None = None
    model: str | None = None
    godot_exe: str | None = None


# ─── Routes ───

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main UI."""
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/status")
async def status(request: Request):
    """Get system status."""
    sess = _get_session(request)
    runner = _get_runner()
    godot_available = runner.is_available() if runner else False
    api_cached = (API_CACHE_DIR / "api_knowledge.json").exists()
    llm_configured = bool(os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY")) or (sess._generator is not None)
    return {
        "godot_available": godot_available,
        "godot_path": runner.exe_path if runner and godot_available else None,
        "api_cached": api_cached,
        "llm_configured": llm_configured,
        "default_output_dir": str(DEFAULT_OUTPUT_DIR if _is_local_request(request) else sess.output_dir),
        "session_id": sess.session_id,
    }


@app.post("/api/config")
async def configure(req: ConfigRequest, request: Request):
    """Configure LLM API key and settings (per-session)."""
    global _runner
    sess = _get_session(request)

    # Update Godot executable path if provided
    if req.godot_exe:
        exe_path = Path(req.godot_exe.strip())
        if exe_path.exists():
            _runner = GodotRunner(str(exe_path))
            logger.info("Godot executable updated: %s", exe_path)
        else:
            raise HTTPException(status_code=400, detail=f"Godot executable not found at: {req.godot_exe}")

    try:
        gen = sess.get_generator(api_key=req.api_key, base_url=req.base_url)
        available_models = []
        try:
            available_models = gen.llm.list_models()
        except Exception as test_err:
            logger.warning(f"Config test - model list failed (may be OK): {test_err}")

        runner = _get_runner()
        return {
            "ok": True,
            "model": gen.llm.model,
            "available_models": available_models,
            "godot_available": runner.is_available() if runner else False,
            "godot_path": runner.exe_path if runner and runner.is_available() else None,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/models")
async def list_models(request: Request, api_key: str | None = None, base_url: str | None = None):
    """List available LLM models (per-session)."""
    sess = _get_session(request)
    try:
        gen = sess.get_generator(api_key=api_key, base_url=base_url)
        models = gen.llm.list_models()
        return {"models": models}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    """Chat with the AI game designer (Agent mode, per-session)."""
    sess = _get_session(request)
    try:
        gen = sess.get_generator(api_key=req.api_key, base_url=req.base_url)
        output_dir = req.output_dir or str(
            (DEFAULT_OUTPUT_DIR if _is_local_request(request) else sess.output_dir) / "_pending"
        )
        result = gen.chat(req.message, output_dir=output_dir)

        response_data: dict[str, Any] = {
            "reply": result["reply"],
            "steps": result.get("steps", []),
        }

        if result.get("plan"):
            plan = result["plan"]
            response_data["project"] = {
                "name": plan.name,
                "game_type": plan.game_type.value,
                "output_dir": plan.output_dir,
                "scenes": [s.filename for s in plan.scenes],
                "scripts": [s.filename for s in plan.scripts],
            }

        if result.get("files"):
            response_data["files"] = result["files"]

        return response_data
    except Exception as e:
        logger.error(f"Chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/generate")
async def generate(req: GenerateRequest, request: Request):
    """Generate a game project from natural language using Agent loop (per-session)."""
    sess = _get_session(request)
    try:
        gen = sess.get_generator(api_key=req.api_key, base_url=req.base_url)
        # Local mode: use global output dir instead of session subdirectory
        base_dir = DEFAULT_OUTPUT_DIR if _is_local_request(request) else sess.output_dir

        if req.project_dir:
            output_dir = req.project_dir
        elif req.output_dir:
            output_dir = req.output_dir
        else:
            output_dir = str(base_dir / "_pending")

        result = gen.agent_generate(req.prompt, output_dir)

        # Rename _pending to actual project name
        if (result.get("project") and not result.get("conversational")
                and not req.project_dir
                and output_dir == str(base_dir / "_pending")):
            project_name = result["project"].get("name", "VibeGame")
            safe_name = "".join(c for c in project_name if c.isalnum() or c in " _-").strip() or "VibeGame"
            new_dir = base_dir / safe_name
            if new_dir.exists():
                import time as _time
                safe_name = f"{safe_name}_{int(_time.time()) % 10000}"
                new_dir = base_dir / safe_name
            pending_path = Path(output_dir)
            if pending_path.exists():
                import shutil
                pending_path.rename(new_dir)
                result["project"]["output_dir"] = str(new_dir)
                result["project"]["name"] = safe_name

        return {
            "ok": result.get("ok", False),
            "project": result.get("project"),
            "files": result.get("files", {}),
            "steps": result.get("steps", []),
            "iterations": result.get("iterations", 0),
            "is_existing": result.get("is_existing", False),
        }
    except Exception as e:
        logger.error(f"Generate error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/generate-stream")
async def generate_stream(req: GenerateRequest, request: Request):
    """Generate a game project with real-time SSE streaming (per-session)."""
    sess = _get_session(request)
    is_local = _is_local_request(request)
    base_dir = DEFAULT_OUTPUT_DIR if is_local else sess.output_dir
    step_queue: queue.Queue = queue.Queue()

    def step_callback(step):
        step_queue.put(step.to_dict())

    def run_agent():
        try:
            gen = sess.get_generator(api_key=req.api_key, base_url=req.base_url)
            if req.project_dir:
                output_dir = req.project_dir
            elif req.output_dir:
                output_dir = req.output_dir
            else:
                output_dir = str(base_dir / "_pending")
            
            result = gen.agent_generate(req.prompt, output_dir, step_callback=step_callback, images=req.images)
            
            # If agent generated a project and output was _pending, rename
            if (result.get("project") and not result.get("conversational")
                    and not req.project_dir
                    and output_dir == str(base_dir / "_pending")):
                project_name = result["project"].get("name", "VibeGame")
                safe_name = "".join(c for c in project_name if c.isalnum() or c in " _-").strip()
                if not safe_name:
                    safe_name = "VibeGame"
                new_dir = base_dir / safe_name
                if new_dir.exists() and new_dir != Path(output_dir):
                    import time as _time
                    safe_name = f"{safe_name}_{int(_time.time()) % 10000}"
                    new_dir = base_dir / safe_name
                pending_path = Path(output_dir)
                if pending_path.exists():
                    import shutil
                    if new_dir.exists():
                        shutil.rmtree(new_dir)
                    pending_path.rename(new_dir)
                    result["project"]["output_dir"] = str(new_dir)
                    result["project"]["name"] = safe_name
                    step_callback_obj = type('Step', (), {'to_dict': lambda self: {"type": "success", "description": f"Project saved as '{safe_name}'", "details": str(new_dir)}})()
                    step_queue.put(step_callback_obj.to_dict())
            
            step_queue.put({"_done": True, "result": result})
        except Exception as e:
            step_queue.put({"_done": True, "error": str(e)})

    # Run agent in background thread
    thread = threading.Thread(target=run_agent, daemon=True)
    thread.start()

    async def event_stream():
        while True:
            try:
                item = step_queue.get(timeout=0.1)
            except queue.Empty:
                # Send keepalive
                yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"
                await asyncio.sleep(0.05)
                continue

            if isinstance(item, dict) and item.get("_done"):
                # Final result
                if "error" in item:
                    yield f"data: {json.dumps({'type': 'error', 'message': item['error']})}\n\n"
                else:
                    result = item["result"]
                    final = {
                        "type": "done",
                        "ok": result.get("ok", False),
                        "project": result.get("project"),
                        "files": result.get("files", {}),
                        "steps": result.get("steps", []),
                        "iterations": result.get("iterations", 0),
                        "is_existing": result.get("is_existing", False),
                        "conversational": result.get("conversational", False),
                        "reply": result.get("reply", ""),
                    }
                    yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
                break
            else:
                yield f"data: {json.dumps({'type': 'step', 'step': item}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.01)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/template")
async def create_from_template(req: TemplateRequest, request: Request):
    """Create a project from a template (per-session)."""
    sess = _get_session(request)
    try:
        template = get_template(req.template_name)
        output_dir = req.output_dir or str(
            (DEFAULT_OUTPUT_DIR if _is_local_request(request) else sess.output_dir) / req.project_name
        )
        plan = template.create_plan(req.project_name, output_dir)
        out = _project_gen.generate(plan)

        files: dict[str, str] = {}
        out_path = Path(out)
        for fpath in out_path.rglob("*"):
            if fpath.is_file() and fpath.suffix in PROJECT_SOURCE_EXTENSIONS:
                try:
                    rel = str(fpath.relative_to(out_path)).replace("\\", "/")
                    files[rel] = fpath.read_text(encoding="utf-8")
                except Exception:
                    pass

        return {
            "ok": True,
            "project": {
                "name": plan.name,
                "game_type": plan.game_type.value,
                "output_dir": out,
                "scenes": [s.filename for s in plan.scenes],
                "scripts": [s.filename for s in plan.scripts],
            },
            "files": files,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Template error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/templates")
async def list_templates():
    """List available game templates."""
    result = []
    for key, tmpl_cls in TEMPLATES.items():
        tmpl = tmpl_cls()
        result.append({
            "id": key,
            "name": tmpl.name,
            "description": tmpl.description,
        })
    return {"templates": result}


@app.get("/api/classes")
async def list_api_classes(category: str | None = None, search: str | None = None, limit: int = 50):
    """Browse the Godot API knowledge base."""
    kb = _get_api_kb()
    if search:
        results = kb.search(search, limit=limit)
    elif category:
        results = kb.get_by_category(category)[:limit]
    else:
        results = list(kb.classes.values())[:limit]

    return {
        "total": len(kb.classes),
        "categories": {k: len(v) for k, v in sorted(kb.categories.items())},
        "results": [
            {
                "name": c.name,
                "inherits": c.inherits,
                "category": c.category,
                "brief": c.brief_description,
                "methods_count": len(c.methods),
                "properties_count": len(c.properties),
                "signals_count": len(c.signals),
            }
            for c in results
        ],
    }


@app.get("/api/classes/{class_name}")
async def get_class_detail(class_name: str):
    """Get detailed info about a specific Godot class."""
    kb = _get_api_kb()
    cls = kb.classes.get(class_name)
    if not cls:
        raise HTTPException(status_code=404, detail=f"Class '{class_name}' not found")

    return {
        "name": cls.name,
        "inherits": cls.inherits,
        "category": cls.category,
        "brief": cls.brief_description,
        "description": cls.description,
        "inheritance_chain": kb.get_inheritance_chain(cls.name),
        "methods": [
            {
                "name": m.name,
                "return_type": m.return_type,
                "description": m.description[:200] if m.description else "",
                "params": [{"name": p.name, "type": p.type, "default": p.default} for p in m.params],
                "qualifiers": m.qualifiers,
            }
            for m in cls.methods
        ],
        "properties": [
            {"name": p.name, "type": p.type, "default": p.default, "description": p.description[:200] if p.description else ""}
            for p in cls.properties
        ],
        "signals": [
            {"name": s.name, "description": s.description[:200] if s.description else "", "params": [{"name": p.name, "type": p.type} for p in s.params]}
            for s in cls.signals
        ],
        "enums": [
            {"name": e.name, "values": [{"name": v.name, "value": v.value} for v in e.values]}
            for e in cls.enums
        ],
    }


def _is_local_request(request: Request) -> bool:
    """Check if the request originates from the local machine."""
    client_ip = request.client.host if request.client else ""
    return client_ip in ("127.0.0.1", "::1", "0.0.0.0", "localhost")


@app.post("/api/open-editor")
async def open_editor(request: Request):
    """Open a project in Godot editor (per-session, tracked). Local only."""
    sess = _get_session(request)
    body = await request.json()
    project_dir = body.get("project_dir")
    if not project_dir:
        raise HTTPException(status_code=400, detail="project_dir required")

    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Cannot open Godot editor remotely. Download the project and open it with your local Godot.")

    if not sess.owns_project(project_dir, local_mode=_is_local_request(request)):
        raise HTTPException(status_code=403, detail="You can only open projects in your own session")

    runner = _get_runner()
    if not runner or not runner.is_available():
        raise HTTPException(status_code=503, detail="Godot executable not found")

    proc = runner.open_editor(project_dir)
    if proc:
        sess.track_pid(proc.pid)
        return {"ok": True, "pid": proc.pid}
    raise HTTPException(status_code=500, detail="Failed to launch editor")


def _is_safe_project_dir(project_dir: str) -> bool:
    """Check that a project directory is under DEFAULT_OUTPUT_DIR (prevents path traversal)."""
    try:
        Path(project_dir).resolve().relative_to(DEFAULT_OUTPUT_DIR.resolve())
        return True
    except ValueError:
        return False


@app.post("/api/run-game")
async def run_game(request: Request):
    """Run a project in game mode.

    - Local requests: launch Godot process directly on the server.
    - Remote requests: return mode='remote' so the frontend shows download options.
    """
    sess = _get_session(request)
    body = await request.json()
    project_dir = body.get("project_dir")
    if not project_dir:
        raise HTTPException(status_code=400, detail="project_dir required")

    # Local user → launch Godot process on this machine
    if _is_local_request(request):
        if not sess.owns_project(project_dir, local_mode=True):
            raise HTTPException(status_code=403, detail="You can only run projects in your own session")
        runner = _get_runner()
        if not runner or not runner.is_available():
            raise HTTPException(status_code=503, detail="Godot executable not found")
        proc = runner.run_project(project_dir)
        if proc:
            sess.track_pid(proc.pid)
            return {"ok": True, "mode": "local", "pid": proc.pid}
        raise HTTPException(status_code=500, detail="Failed to launch game")

    # Remote user → return download link so they can run locally
    if not _is_safe_project_dir(project_dir):
        raise HTTPException(status_code=403, detail="Invalid project directory")

    p = Path(project_dir).resolve()
    if not p.exists() or not (p / "project.godot").exists():
        raise HTTPException(status_code=404, detail="Project not found")

    # Build a download URL using the encoded project path
    from urllib.parse import quote
    return {
        "ok": True,
        "mode": "remote",
        "project_name": p.name,
        "download_project_url": f"/api/download-project?project_dir={quote(str(p), safe='')}",
    }


@app.post("/api/stop-game")
async def stop_game(request: Request):
    """Kill all tracked Godot processes for this session."""
    sess = _get_session(request)
    sess.kill_all_processes()
    return {"ok": True}


@app.get("/api/session")
async def session_info(request: Request):
    """Get current session info."""
    sess = _get_session(request)
    return {
        "session_id": sess.session_id,
        "output_dir": str(sess.output_dir),
        "created": sess.created,
        "last_active": sess.last_active,
        "tracked_pids": list(sess._pids),
    }


@app.get("/api/web-export-status")
async def web_export_status():
    """Check if Web export templates are installed."""
    runner = _get_runner()
    if not runner.is_available():
        return {"available": False, "reason": "Godot executable not found"}
    status = runner.get_web_template_status()
    return {"available": status["installed"], "path": status["path"], "hint": status.get("hint")}


@app.post("/api/export-web")
async def export_web(request: Request):
    """Export a project as HTML5/Web for browser play (per-session)."""
    sess = _get_session(request)
    body = await request.json()
    project_dir = body.get("project_dir")
    if not project_dir:
        raise HTTPException(status_code=400, detail="project_dir required")
    if not sess.owns_project(project_dir, local_mode=_is_local_request(request)) and not _is_safe_project_dir(project_dir):
        raise HTTPException(status_code=403, detail="Invalid project directory")

    runner = _get_runner()
    if not runner.is_available():
        raise HTTPException(status_code=503, detail="Godot executable not found")

    # Export into project_dir/_web_export/
    result = runner.export_project_web(project_dir)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", "Export failed"))

    # Use session-scoped play URL: /play/<session_id>/<project_name>/
    dir_name = Path(project_dir).name
    return {
        "ok": True,
        "export_dir": result["export_dir"],
        "play_url": f"/play/{sess.session_id}/{dir_name}/",
        "files": result.get("files", []),
        "message": result.get("message", ""),
    }


@app.get("/api/windows-export-status")
async def windows_export_status():
    """Check if Windows Desktop export templates are installed."""
    runner = _get_runner()
    if not runner.is_available():
        return {"available": False, "reason": "Godot executable not found"}
    status = runner.get_windows_template_status()
    return {"available": status["installed"], "path": status["path"], "hint": status.get("hint")}


@app.post("/api/export-windows")
async def export_windows(request: Request):
    """Export a project as Windows .exe for download (per-session)."""
    sess = _get_session(request)
    body = await request.json()
    project_dir = body.get("project_dir")
    if not project_dir:
        raise HTTPException(status_code=400, detail="project_dir required")
    if not sess.owns_project(project_dir, local_mode=_is_local_request(request)) and not _is_safe_project_dir(project_dir):
        raise HTTPException(status_code=403, detail="Invalid project directory")

    runner = _get_runner()
    if not runner.is_available():
        raise HTTPException(status_code=503, detail="Godot executable not found")

    result = runner.export_project_windows(project_dir)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", "Windows export failed"))

    dir_name = Path(project_dir).name
    zip_name = result.get("zip_name", f"{dir_name}.zip")
    download_url = f"/api/download/{sess.session_id}/{dir_name}/{zip_name}"
    return {
        "ok": True,
        "export_dir": result["export_dir"],
        "download_url": download_url,
        "zip_name": zip_name,
        "files": result.get("files", []),
        "message": result.get("message", ""),
    }


@app.get("/api/download/{session_id}/{dir_name}/{filename}")
async def download_export(session_id: str, dir_name: str, filename: str):
    """Download an exported file (Windows .zip) for a session's project."""
    from fastapi.responses import FileResponse

    sess = _session_mgr.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")

    # Look in _win_export directory (try session dir first, then global dir)
    export_dir = sess.output_dir / dir_name / "_win_export"
    if not export_dir.exists():
        export_dir = DEFAULT_OUTPUT_DIR / dir_name / "_win_export"
    if not export_dir.exists():
        raise HTTPException(status_code=404, detail="Export directory not found")

    target = _resolve_safe_path(str(export_dir), filename)
    if target is None or not target.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        target,
        media_type="application/zip",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/download-project")
async def download_project(project_dir: str):
    """Package a project's source files into a zip and serve for download.

    The zip contains all project files (scenes, scripts, assets, project.godot)
    so the user can open it with a local Godot installation.
    Accepts project_dir as a query parameter. Validates it is under DEFAULT_OUTPUT_DIR.
    """
    import zipfile
    import io
    from fastapi.responses import StreamingResponse as _SR

    if not _is_safe_project_dir(project_dir):
        raise HTTPException(status_code=403, detail="Invalid project directory")

    p = Path(project_dir).resolve()
    if not p.exists() or not (p / "project.godot").exists():
        raise HTTPException(status_code=404, detail="Project not found")

    dir_name = p.name

    # Build zip in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in sorted(p.rglob("*")):
            if not fpath.is_file():
                continue
            rel = fpath.relative_to(p)
            rel_str = str(rel).replace("\\", "/")
            # Skip export artifacts, .godot cache, and export config
            if rel_str.startswith(("_web_export/", "_win_export/", ".godot/")):
                continue
            if rel_str == "export_presets.cfg":
                continue
            # Write into a top-level folder matching the project name
            zf.write(fpath, f"{dir_name}/{rel_str}")
    buf.seek(0)
    zip_name = f"{dir_name}.zip"

    return _SR(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@app.get("/play/{session_id}/{dir_name}/{file_path:path}")
async def play_game_files(session_id: str, dir_name: str, file_path: str = ""):
    """Serve exported Web game files (session-scoped)."""
    from fastapi.responses import FileResponse

    sess = _session_mgr.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")

    export_dir = sess.output_dir / dir_name / "_web_export"
    if not export_dir.exists():
        export_dir = DEFAULT_OUTPUT_DIR / dir_name / "_web_export"
    if not export_dir.exists():
        raise HTTPException(status_code=404, detail="Export directory not found")

    # Serve index.html when no file_path or trailing slash
    if not file_path or file_path == "/":
        target = export_dir / "index.html"
    else:
        target = _resolve_safe_path(str(export_dir), file_path)

    if target is None or not target.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Correct MIME types for Godot Web export files
    mime_map = {
        ".wasm": "application/wasm",
        ".pck": "application/octet-stream",
        ".js": "application/javascript",
        ".html": "text/html",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }
    media_type = mime_map.get(target.suffix.lower(), "application/octet-stream")

    # SharedArrayBuffer requires cross-origin isolation headers
    headers = {
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Embedder-Policy": "require-corp",
    }
    return FileResponse(target, media_type=media_type, headers=headers)


@app.post("/api/reset-chat")
async def reset_chat(request: Request):
    """Reset the chat history for a specific project (per-session)."""
    sess = _get_session(request)
    gen = sess._generator
    if gen:
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        project_dir = body.get("project_dir")
        if project_dir:
            gen.reset_chat(project_dir)
        else:
            gen.clear_all_chat()
    return {"ok": True}


@app.get("/api/project-files")
async def get_project_files(project_dir: str):
    """Get all files content of a project (for loading into the file panel)."""
    path = Path(project_dir)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Project directory not found")
    
    files: dict[str, str] = {}
    for fpath in path.rglob("*"):
        if fpath.is_file() and fpath.suffix in PROJECT_SOURCE_EXTENSIONS:
            try:
                rel = str(fpath.relative_to(path)).replace("\\", "/")
                if not is_ignored_path(rel):
                    files[rel] = fpath.read_text(encoding="utf-8")
            except Exception:
                pass
    
    return {"files": files, "project_dir": str(path)}


# ─── Asset Upload ───

# Supported asset extensions for Godot projects (sourced from models.py + extras)
ASSET_EXTENSIONS = ASSET_EXTENSIONS_ALL | frozenset({
    # Godot resources
    ".tres", ".tscn", ".gdshader", ".gd",
    # Other
    ".json", ".cfg", ".txt", ".csv",
})


@app.post("/api/assets/upload")
async def upload_assets(
    project_dir: str = Form(...),
    subfolder: str = Form(default=""),
    files: list[UploadFile] = File(...),
):
    """Upload asset files (images, audio, fonts, 3D models, etc.) to a project directory.
    
    Files are saved to project_dir/[subfolder]/ and will be available as res://[subfolder]/filename
    in Godot. If no project exists yet, creates the directory structure.
    """
    project_path = Path(project_dir)
    
    # Determine target directory
    if subfolder:
        target_dir = project_path / subfolder
    else:
        target_dir = project_path / "assets"
    
    target_dir.mkdir(parents=True, exist_ok=True)
    
    uploaded = []
    errors = []
    
    for file in files:
        ext = Path(file.filename).suffix.lower()
        if ext not in ASSET_EXTENSIONS:
            errors.append(f"Unsupported file type: {file.filename} ({ext})")
            continue
        
        # Sanitize filename — strip path components, keep only the base name
        safe_name = Path(file.filename).name
        # Reject hidden files
        if safe_name.startswith("."):
            errors.append(f"Hidden files not allowed: {safe_name}")
            continue
        
        target_path = _resolve_safe_path(str(project_path), str(Path(target_dir.relative_to(project_path)) / safe_name))
        if target_path is None:
            errors.append(f"Invalid path for {safe_name}")
            continue
        
        try:
            content = await file.read()
            with open(target_path, "wb") as f:
                f.write(content)
            
            rel_path = str(target_path.relative_to(project_path)).replace("\\", "/")
            uploaded.append({
                "filename": safe_name,
                "path": rel_path,
                "res_path": f"res://{rel_path}",
                "size": len(content),
                "type": ext,
            })
        except Exception as e:
            errors.append(f"Failed to save {file.filename}: {e}")
    
    return {
        "ok": len(uploaded) > 0,
        "uploaded": uploaded,
        "errors": errors,
        "target_dir": str(target_dir),
    }


@app.get("/api/assets/list")
async def list_assets(project_dir: str):
    """List all asset files in a project directory (non-text resources like images, audio, models)."""
    project_path = Path(project_dir)
    if not project_path.exists():
        return {"assets": [], "project_dir": project_dir}
    
    assets = []
    for fpath in sorted(project_path.rglob("*")):
        if not fpath.is_file():
            continue
        rel = str(fpath.relative_to(project_path)).replace("\\", "/")
        if is_ignored_path(rel):
            continue
        
        ext = fpath.suffix.lower()
        if ext in ASSET_EXTENSIONS_ALL:
            category = classify_asset(ext)
            assets.append({
                "filename": fpath.name,
                "path": rel,
                "res_path": f"res://{rel}",
                "size": fpath.stat().st_size,
                "category": category,
                "type": ext,
            })
    
    return {"assets": assets, "project_dir": project_dir}


@app.delete("/api/assets")
async def delete_asset(request: Request):
    """Delete an asset file from a project."""
    body = await request.json()
    project_dir = body.get("project_dir")
    asset_path = body.get("asset_path")  # relative path like "assets/player.png"
    
    if not project_dir or not asset_path:
        raise HTTPException(status_code=400, detail="project_dir and asset_path required")
    
    filepath = _resolve_safe_path(project_dir, asset_path)
    if filepath is None:
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Asset not found")
    
    filepath.unlink()
    return {"ok": True}


@app.get("/api/assets/preview")
async def preview_asset(project_dir: str, path: str):
    """Serve an asset file for preview (images only for security)."""
    from fastapi.responses import FileResponse
    
    filepath = _resolve_safe_path(project_dir, path)
    if filepath is None:
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Asset not found")
    
    # Only serve image types for preview
    ext = filepath.suffix.lower()
    mime_types = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".svg": "image/svg+xml", ".bmp": "image/bmp",
        ".gif": "image/gif",
    }
    media_type = mime_types.get(ext)
    if not media_type:
        raise HTTPException(status_code=415, detail="Not a previewable image type")
    
    return FileResponse(filepath, media_type=media_type)


@app.get("/api/project-memory")
async def get_project_memory(project_dir: str):
    """Get the memory.md content of a project."""
    memory_path = Path(project_dir) / "memory.md"
    if not memory_path.exists():
        return {"memory": None, "exists": False}
    try:
        content = memory_path.read_text(encoding="utf-8")
        return {"memory": content, "exists": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/project-memory")
async def update_project_memory(request: Request):
    """Update the memory.md content of a project."""
    body = await request.json()
    project_dir = body.get("project_dir")
    content = body.get("content", "")
    if not project_dir:
        raise HTTPException(status_code=400, detail="project_dir required")

    memory_path = Path(project_dir) / "memory.md"
    try:
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        with open(memory_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/projects")
async def list_projects(request: Request, output_dir: str | None = None):
    """List all generated projects.
    
    Local mode: scans DEFAULT_OUTPUT_DIR and all subdirectories for project.godot,
    so projects from any session are visible (single-user local dev).
    Remote mode: only lists projects in the session's own output directory.
    """
    sess = _get_session(request)
    is_local = _is_local_request(request)

    if output_dir:
        base = Path(output_dir)
    elif is_local:
        # Local user: show ALL projects under the global output dir
        base = DEFAULT_OUTPUT_DIR
    else:
        base = sess.output_dir

    if not base.exists():
        return {"projects": []}

    projects = []

    def _collect_projects(directory: Path, depth: int = 0):
        """Recursively find project.godot in directory (max 2 levels deep)."""
        if depth > 2:
            return
        try:
            items = sorted(directory.iterdir(), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
        except OSError:
            return
        for item in items:
            if not item.is_dir():
                continue
            if (item / "project.godot").exists():
                try:
                    stat = item.stat()
                    file_count = sum(
                        1 for f in item.rglob("*")
                        if f.is_file() and f.suffix in PROJECT_SOURCE_EXTENSIONS
                    )
                    has_memory = (item / "memory.md").exists()
                    has_chat = (item / "chat_history.json").exists()
                    assets_dir = item / "assets"
                    has_assets = assets_dir.is_dir() and any(assets_dir.iterdir())
                    projects.append({
                        "name": item.name,
                        "path": str(item),
                        "modified": stat.st_mtime,
                        "file_count": file_count,
                        "has_memory": has_memory,
                        "has_chat": has_chat,
                        "has_assets": has_assets,
                    })
                except OSError:
                    continue
            elif is_local and depth < 2:
                # For local mode, recurse into subdirs (session dirs) to find projects
                _collect_projects(item, depth + 1)

    _collect_projects(base)

    # Deduplicate by path and sort by modified time
    seen = set()
    unique_projects = []
    for p in sorted(projects, key=lambda x: x["modified"], reverse=True):
        if p["path"] not in seen:
            seen.add(p["path"])
            unique_projects.append(p)

    return {"projects": unique_projects}


@app.delete("/api/projects")
async def delete_project(request: Request):
    """Delete a generated project (per-session, ownership check)."""
    import shutil
    sess = _get_session(request)
    body = await request.json()
    project_dir = body.get("project_dir")
    if not project_dir:
        raise HTTPException(status_code=400, detail="project_dir required")

    path = Path(project_dir)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Project not found")

    # Safety: only allow deleting inside the session's output dir
    if not sess.owns_project(project_dir, local_mode=_is_local_request(request)):
        raise HTTPException(status_code=403, detail="Can only delete projects within your own session")

    shutil.rmtree(path)
    return {"ok": True}


# ─── Project Chat History (stored inside project directory) ───

def _project_chat_path(project_dir: str) -> Path:
    return Path(project_dir) / "chat_history.json"


def _load_project_chat(project_dir: str) -> dict:
    """Load chat history from project directory."""
    path = _project_chat_path(project_dir)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"messages": [], "created": time.time(), "updated": time.time()}


def _save_project_chat(project_dir: str, data: dict):
    """Save chat history to project directory."""
    path = _project_chat_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated"] = time.time()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/project-chat")
async def get_project_chat(project_dir: str):
    """Get chat history for a project."""
    data = _load_project_chat(project_dir)
    return data


@app.post("/api/project-chat/message")
async def append_project_chat_message(request: Request):
    """Append a message to project chat history."""
    body = await request.json()
    project_dir = body.get("project_dir")
    if not project_dir:
        raise HTTPException(status_code=400, detail="project_dir required")
    
    data = _load_project_chat(project_dir)
    msg = {
        "role": body.get("role", "user"),
        "content": body.get("content", ""),
        "timestamp": time.time(),
    }
    if body.get("project"):
        msg["project"] = body["project"]
    if body.get("steps"):
        msg["steps"] = body["steps"]
    
    data["messages"].append(msg)
    _save_project_chat(project_dir, data)
    return {"ok": True}


@app.delete("/api/project-chat")
async def clear_project_chat(request: Request):
    """Clear chat history for a project."""
    body = await request.json()
    project_dir = body.get("project_dir")
    if not project_dir:
        raise HTTPException(status_code=400, detail="project_dir required")
    
    path = _project_chat_path(project_dir)
    if path.exists():
        path.unlink()
    return {"ok": True}


@app.post("/api/projects/create")
async def create_project(request: Request):
    """Create a new empty project directory with project.godot."""
    sess = _get_session(request)
    body = await request.json()
    name = body.get("name", "MyGame")
    # Local mode: default to global output dir (no session subdirectory)
    if body.get("output_dir"):
        output_dir = body["output_dir"]
    elif _is_local_request(request):
        output_dir = str(DEFAULT_OUTPUT_DIR)
    else:
        output_dir = str(sess.output_dir)
    
    # Sanitize name
    safe_name = "".join(c for c in name if c.isalnum() or c in " _-").strip() or "MyGame"
    project_path = Path(output_dir) / safe_name
    
    # If exists, add suffix
    if project_path.exists():
        safe_name = f"{safe_name}_{int(time.time()) % 10000}"
        project_path = Path(output_dir) / safe_name
    
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / "assets").mkdir(exist_ok=True)
    
    # Create minimal project.godot
    project_godot = project_path / "project.godot"
    project_godot.write_text(
        f'; Engine configuration file.\n'
        f'config_version=5\n\n'
        f'[application]\n\n'
        f'config/name="{safe_name}"\n'
        f'config/features=PackedStringArray("4.4")\n',
        encoding="utf-8"
    )
    
    # Initialize empty chat history
    _save_project_chat(str(project_path), {
        "messages": [],
        "created": time.time(),
        "updated": time.time(),
    })
    
    return {
        "ok": True,
        "project": {
            "name": safe_name,
            "path": str(project_path),
        }
    }


# ─── Legacy Conversation Management (kept for backward compat) ───

def _ensure_conversations_dir():
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)


def _conv_path(conv_id: str) -> Path:
    return CONVERSATIONS_DIR / f"{conv_id}.json"


@app.get("/api/conversations")
async def list_conversations():
    _ensure_conversations_dir()
    conversations = []
    for f in CONVERSATIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            conversations.append({
                "id": data.get("id", f.stem),
                "title": data.get("title", "Untitled"),
                "created": data.get("created", 0),
                "updated": data.get("updated", 0),
                "message_count": len(data.get("messages", [])),
                "project_dir": data.get("project_dir"),
            })
        except Exception:
            pass
    conversations.sort(key=lambda c: c["updated"], reverse=True)
    return {"conversations": conversations}


@app.post("/api/conversations")
async def create_conversation(request: Request):
    _ensure_conversations_dir()
    body = await request.json()
    conv_id = str(uuid.uuid4())[:8]
    now = time.time()
    conv = {
        "id": conv_id,
        "title": body.get("title", "New Chat"),
        "created": now,
        "updated": now,
        "messages": [],
        "project_dir": body.get("project_dir"),
    }
    _conv_path(conv_id).write_text(json.dumps(conv, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "conversation": conv}


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    path = _conv_path(conv_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Conversation not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    path = _conv_path(conv_id)
    if path.exists():
        path.unlink()
    return {"ok": True}


def start_server(host: str = "127.0.0.1", port: int = 8899):
    """Start the web server."""
    import uvicorn
    print(f"\n  GodotVibe Web UI starting at http://{host}:{port}")
    print(f"  Press Ctrl+C to stop\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
