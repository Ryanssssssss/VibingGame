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
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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
from vibe_tools.llm_provider import GameGenerator
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Global state
_api_kb: APIKnowledgeBase | None = None
_runner: GodotRunner | None = None
_generator: GameGenerator | None = None
_project_gen = ProjectGenerator()
_generator_lock = threading.Lock()  # Protect _generator creation (not needed per-request)


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


def _get_runner() -> GodotRunner:
    global _runner
    if _runner is None:
        _runner = GodotRunner(str(GODOT_EXE) if GODOT_EXE.exists() else None)
    return _runner


_last_config: dict[str, str | None] = {"api_key": None, "base_url": None, "model": None}


def _get_generator(api_key: str | None = None, base_url: str | None = None, model: str | None = None) -> GameGenerator:
    global _generator, _last_config
    effective_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY")
    effective_url = base_url or os.getenv("LLM_BASE_URL") or os.getenv("GEMINI_BASE_URL")
    effective_model = model or os.getenv("LLM_MODEL") or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    with _generator_lock:
        config_changed = (
            _generator is None
            or effective_key != _last_config["api_key"]
            or effective_url != _last_config["base_url"]
            or effective_model != _last_config["model"]
        )

        if config_changed:
            _generator = GameGenerator(
                model=effective_model,
                api_key=effective_key,
                base_url=effective_url,
            )
            _last_config = {"api_key": effective_key, "base_url": effective_url, "model": effective_model}

        return _generator


# ─── Pydantic Models ───

class ChatRequest(BaseModel):
    message: str
    output_dir: str | None = None
    project_dir: str | None = None  # Explicit project dir for continuing conversations
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class GenerateRequest(BaseModel):
    prompt: str
    output_dir: str | None = None
    project_dir: str | None = None  # Explicit project dir for continuing conversations
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    images: list[str] | None = None  # Base64 data URIs for vision (screenshots / pasted images)


class TemplateRequest(BaseModel):
    template_name: str
    project_name: str
    output_dir: str | None = None


class ConfigRequest(BaseModel):
    api_key: str
    base_url: str | None = None
    model: str | None = None


# ─── Routes ───

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main UI."""
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/status")
async def status():
    """Get system status."""
    godot_available = GODOT_EXE.exists()
    api_cached = (API_CACHE_DIR / "api_knowledge.json").exists()
    llm_configured = bool(os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY")) or (_generator is not None)
    return {
        "godot_available": godot_available,
        "godot_path": str(GODOT_EXE) if godot_available else None,
        "api_cached": api_cached,
        "llm_configured": llm_configured,
        "default_output_dir": str(DEFAULT_OUTPUT_DIR),
    }


@app.post("/api/config")
async def configure(req: ConfigRequest):
    """Configure LLM API key and settings."""
    try:
        gen = _get_generator(api_key=req.api_key, base_url=req.base_url, model=req.model)
        # Quick connectivity test: list models
        available_models = []
        try:
            available_models = gen.llm.list_models()
        except Exception as test_err:
            logger.warning(f"Config test - model list failed (may be OK): {test_err}")
        return {"ok": True, "model": gen.llm.model, "available_models": available_models}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/models")
async def list_models(api_key: str | None = None, base_url: str | None = None):
    """List available LLM models."""
    try:
        gen = _get_generator(api_key=api_key, base_url=base_url)
        models = gen.llm.list_models()
        return {"models": models}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Chat with the AI game designer (Agent mode)."""
    try:
        gen = _get_generator(api_key=req.api_key, base_url=req.base_url, model=req.model)
        output_dir = req.output_dir or str(DEFAULT_OUTPUT_DIR / "_pending")
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
async def generate(req: GenerateRequest):
    """Generate a game project from natural language using Agent loop."""
    try:
        gen = _get_generator(api_key=req.api_key, base_url=req.base_url, model=req.model)

        # Determine output directory:
        # 1. If project_dir is set, continue working on that project
        # 2. If output_dir is set, use it
        # 3. Default to latest
        if req.project_dir:
            output_dir = req.project_dir
        elif req.output_dir:
            output_dir = req.output_dir
        else:
            output_dir = str(DEFAULT_OUTPUT_DIR / "_pending")

        # Use the agent loop
        result = gen.agent_generate(req.prompt, output_dir)

        # Rename _pending to actual project name
        if (result.get("project") and not result.get("conversational")
                and not req.project_dir
                and output_dir == str(DEFAULT_OUTPUT_DIR / "_pending")):
            project_name = result["project"].get("name", "VibeGame")
            safe_name = "".join(c for c in project_name if c.isalnum() or c in " _-").strip() or "VibeGame"
            new_dir = DEFAULT_OUTPUT_DIR / safe_name
            if new_dir.exists():
                import time as _time
                safe_name = f"{safe_name}_{int(_time.time()) % 10000}"
                new_dir = DEFAULT_OUTPUT_DIR / safe_name
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
async def generate_stream(req: GenerateRequest):
    """Generate a game project with real-time SSE streaming of agent steps.
    
    All messages go through agent_generate() — the LLM decides whether to
    chat (text reply) or act (tool calls) based on context.
    """
    step_queue: queue.Queue = queue.Queue()

    def step_callback(step):
        step_queue.put(step.to_dict())

    def run_agent():
        try:
            gen = _get_generator(api_key=req.api_key, base_url=req.base_url, model=req.model)
            if req.project_dir:
                output_dir = req.project_dir
            elif req.output_dir:
                output_dir = req.output_dir
            else:
                output_dir = str(DEFAULT_OUTPUT_DIR / "_pending")
            
            # Unified agent loop — handles both chat and action
            result = gen.agent_generate(req.prompt, output_dir, step_callback=step_callback, images=req.images)
            
            # If agent generated a project and output was _pending, rename to actual project name
            if (result.get("project") and not result.get("conversational")
                    and not req.project_dir
                    and output_dir == str(DEFAULT_OUTPUT_DIR / "_pending")):
                project_name = result["project"].get("name", "VibeGame")
                # Sanitize name for filesystem
                safe_name = "".join(c for c in project_name if c.isalnum() or c in " _-").strip()
                if not safe_name:
                    safe_name = "VibeGame"
                new_dir = DEFAULT_OUTPUT_DIR / safe_name
                # If dir already exists, add suffix
                if new_dir.exists() and new_dir != Path(output_dir):
                    import time as _time
                    safe_name = f"{safe_name}_{int(_time.time()) % 10000}"
                    new_dir = DEFAULT_OUTPUT_DIR / safe_name
                # Move the project
                pending_path = Path(output_dir)
                if pending_path.exists():
                    import shutil
                    if new_dir.exists():
                        shutil.rmtree(new_dir)
                    pending_path.rename(new_dir)
                    result["project"]["output_dir"] = str(new_dir)
                    result["project"]["name"] = safe_name
                    # Update files paths if needed
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
async def create_from_template(req: TemplateRequest):
    """Create a project from a template."""
    try:
        template = get_template(req.template_name)
        output_dir = req.output_dir or str(DEFAULT_OUTPUT_DIR / req.project_name)
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


@app.post("/api/open-editor")
async def open_editor(request: Request):
    """Open a project in Godot editor."""
    body = await request.json()
    project_dir = body.get("project_dir")
    if not project_dir:
        raise HTTPException(status_code=400, detail="project_dir required")

    runner = _get_runner()
    if not runner.is_available():
        raise HTTPException(status_code=503, detail="Godot executable not found")

    proc = runner.open_editor(project_dir)
    if proc:
        return {"ok": True, "pid": proc.pid}
    raise HTTPException(status_code=500, detail="Failed to launch editor")


@app.post("/api/run-game")
async def run_game(request: Request):
    """Run a project in game mode."""
    body = await request.json()
    project_dir = body.get("project_dir")
    if not project_dir:
        raise HTTPException(status_code=400, detail="project_dir required")

    runner = _get_runner()
    if not runner.is_available():
        raise HTTPException(status_code=503, detail="Godot executable not found")

    proc = runner.run_project(project_dir)
    if proc:
        return {"ok": True, "pid": proc.pid}
    raise HTTPException(status_code=500, detail="Failed to launch game")


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
    """Export a project as HTML5/Web for browser play."""
    body = await request.json()
    project_dir = body.get("project_dir")
    if not project_dir:
        raise HTTPException(status_code=400, detail="project_dir required")

    runner = _get_runner()
    if not runner.is_available():
        raise HTTPException(status_code=503, detail="Godot executable not found")

    # Export into project_dir/_web_export/
    result = runner.export_project_web(project_dir)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", "Export failed"))

    # Use path-based URL so relative asset references (index.js, index.wasm, etc.) work
    dir_name = Path(project_dir).name
    return {
        "ok": True,
        "export_dir": result["export_dir"],
        "play_url": f"/play/{dir_name}/",
        "files": result.get("files", []),
        "message": result.get("message", ""),
    }


@app.get("/play/{dir_name}/{file_path:path}")
async def play_game_files(dir_name: str, file_path: str = ""):
    """Serve exported Web game files (index.html and all assets).

    Uses path prefix /play/<project_name>/ so that relative asset references
    (index.js, index.wasm, index.pck, etc.) resolve correctly.
    """
    from fastapi.responses import FileResponse

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
    """Reset the chat history for a specific project."""
    if _generator:
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        project_dir = body.get("project_dir")
        if project_dir:
            _generator.reset_chat(project_dir)
        else:
            _generator.clear_all_chat()
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
async def list_projects(output_dir: str | None = None):
    """List all generated projects in the output directory."""
    base = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    if not base.exists():
        return {"projects": []}

    projects = []
    try:
        items = sorted(base.iterdir(), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
    except OSError:
        return {"projects": []}

    for item in items:
        if not item.is_dir():
            continue
        if not (item / "project.godot").exists():
            continue
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

    return {"projects": projects}


@app.delete("/api/projects")
async def delete_project(request: Request):
    """Delete a generated project."""
    import shutil
    body = await request.json()
    project_dir = body.get("project_dir")
    if not project_dir:
        raise HTTPException(status_code=400, detail="project_dir required")

    path = Path(project_dir)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Project not found")

    # Safety: only allow deleting inside the default output dir
    try:
        path.relative_to(DEFAULT_OUTPUT_DIR)
    except ValueError:
        raise HTTPException(status_code=403, detail="Can only delete projects within the output directory")

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
    body = await request.json()
    name = body.get("name", "MyGame")
    output_dir = body.get("output_dir") or str(DEFAULT_OUTPUT_DIR)
    
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
