"""Authenticated loopback service used by the built-in Godot editor dock."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# Allow the bundled entry point to be launched by absolute path.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from vibe_tools.agent import ChatHistoryStore, GameGenerator
from vibe_tools.editor_credentials import credential
from vibe_tools.editor_workspace import Conflict, Workspace, rollback, safe_path
from vibe_tools.godot_runner import GodotRunner
from vibe_tools.project_generator import ProjectGenerator
from vibe_tools.templates import TEMPLATES, get_template

log = logging.getLogger("vibe_editor")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class Runtime:
    project = Path()
    state = Path()
    token = ""
    godot_exe = ""
    tasks: dict[str, dict[str, Any]] = {}
    locks: dict[str, threading.Lock] = {}
    play_tokens: dict[str, Path] = {}
    loopback_url = ""
    mutex = threading.RLock()


class ConfigRequest(BaseModel):
    api_key: str | None = None
    base_url: str = ""
    model: str = "claude-sonnet-4-20250514"
    vision_model: str = "gemini-2.5-flash"


class TaskRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    project_dir: str | None = None
    dirty_files: list[str] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)


class TemplateRequest(BaseModel):
    template: str
    name: str = Field(min_length=1, max_length=80)
    project_dir: str
    dirty_files: list[str] = Field(default_factory=list)


class RollbackRequest(BaseModel):
    task_id: str
    project_dir: str | None = None
    dirty_files: list[str] = Field(default_factory=list)


@app.middleware("http")
async def authenticate(request, call_next):
    if request.url.path.startswith("/play/"):
        return await call_next(request)
    if request.headers.get("authorization") != "Bearer " + Runtime.token:
        return JSONResponse({"detail": "Invalid Vibe Agent session"}, status_code=401)
    return await call_next(request)


def _config_path() -> Path:
    return Runtime.state / "config.json"


def _config() -> dict[str, str]:
    defaults = {"base_url": "", "model": "claude-sonnet-4-20250514", "vision_model": "gemini-2.5-flash"}
    try:
        loaded = json.loads(_config_path().read_text(encoding="utf-8"))
        defaults.update({k: str(v) for k, v in loaded.items() if k in defaults})
    except (OSError, ValueError):
        pass
    return defaults


def _save_config(value: dict[str, str]):
    Runtime.state.mkdir(parents=True, exist_ok=True)
    temporary = _config_path().with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, _config_path())


def _root(value: str | None) -> Path:
    root = Path(value).resolve() if value else Runtime.project
    root.mkdir(parents=True, exist_ok=True)
    return root


def _history_file(root: Path) -> Path:
    key = hashlib.sha256(str(root.resolve()).lower().encode("utf-8")).hexdigest()
    return Runtime.state / "history" / f"{key}.json"


def _emit(task: dict, kind: str, description: str, details: str = ""):
    with Runtime.mutex:
        task["events"].append({"sequence": len(task["events"]), "type": kind,
                               "description": description, "details": details})


def _attachments(paths: list[str]) -> list[str]:
    result = []
    allowed = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif"}
    for raw in paths[:8]:
        path = Path(raw).resolve()
        mime = allowed.get(path.suffix.lower())
        if not mime or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError(f"不支持的附件：{path.name}")
        result.append(f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}")
    return result


def _new_task(root: Path) -> tuple[str, dict]:
    task_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    task = {"id": task_id, "project_dir": str(root), "status": "queued", "events": [],
            "changed_files": [], "reply": "", "error": "", "cancel": threading.Event()}
    with Runtime.mutex:
        if len(Runtime.tasks) >= 100:
            for old_id in list(Runtime.tasks):
                if Runtime.tasks[old_id]["status"] in ("done", "error", "cancelled"):
                    Runtime.tasks.pop(old_id)
                    if len(Runtime.tasks) < 80:
                        break
        Runtime.tasks[task_id] = task
    return task_id, task


def _runner() -> GodotRunner:
    return GodotRunner(Runtime.godot_exe or None)


def _run_agent(task: dict, request: TaskRequest):
    root = Path(task["project_dir"])
    lock = Runtime.locks.setdefault(str(root).lower(), threading.Lock())
    workspace = None
    with lock:
        task["status"] = "running"
        try:
            workspace = Workspace(root, Runtime.state, task["id"])
            config = _config()
            api_key = credential("api_key")
            if not api_key:
                raise ValueError("请先在 Vibe Agent 设置中保存 API Key")
            # The vision helper reads this module constant.
            import vibe_tools.agent as agent_module
            agent_module.VISION_MODEL = config["vision_model"]
            generator = GameGenerator(api_key=api_key, base_url=config["base_url"] or None,
                                      model=config["model"],
                                      history_file=str(_history_file(root)),
                                      runner=_runner(), cancel_event=task["cancel"], history_key=str(root))

            def step_callback(step):
                if task["cancel"].is_set():
                    raise InterruptedError("任务已取消")
                changed = workspace.commit(request.dirty_files)
                if changed:
                    task["changed_files"].extend(p for p in changed if p not in task["changed_files"])
                    _emit(task, "files_changed", "已应用文件变更", json.dumps(changed, ensure_ascii=False))
                value = step.to_dict()
                _emit(task, value.get("type", "step"), value.get("description", ""), value.get("details", ""))

            result = generator.agent_generate(request.prompt, str(workspace.stage), step_callback,
                                              _attachments(request.attachments))
            changed = workspace.commit(request.dirty_files)
            task["changed_files"].extend(p for p in changed if p not in task["changed_files"])
            task["reply"] = result.get("reply", "")
            task["result"] = {k: v for k, v in result.items() if k not in ("files", "steps")}
            if isinstance(task["result"].get("project"), dict):
                task["result"]["project"]["output_dir"] = str(root)
            web_index = root / "_web_export" / "index.html"
            if web_index.is_file() and any(name.startswith("_web_export/") for name in task["changed_files"]):
                play_token = uuid.uuid4().hex
                Runtime.play_tokens[play_token] = web_index.parent
                _emit(task, "open_url", "在浏览器中打开 Web 导出",
                      f"{Runtime.loopback_url}/play/{play_token}/index.html")
            task["status"] = "cancelled" if task["cancel"].is_set() else "done"
        except InterruptedError as error:
            task["status"], task["error"] = "cancelled", str(error)
        except Exception as error:
            task["status"], task["error"] = "error", str(error)
            log.exception("Editor agent task failed")
        finally:
            if workspace:
                workspace.close()
            _emit(task, task["status"], "任务结束", task["error"] or task["reply"])


def _run_template(task: dict, request: TemplateRequest):
    root = Path(task["project_dir"])
    lock = Runtime.locks.setdefault(str(root).lower(), threading.Lock())
    workspace = None
    with lock:
        task["status"] = "running"
        try:
            if task["cancel"].is_set():
                raise InterruptedError("任务已取消")
            workspace = Workspace(root, Runtime.state, task["id"])
            plan = get_template(request.template).create_plan(request.name, str(workspace.stage))
            ProjectGenerator().generate(plan)
            if task["cancel"].is_set():
                raise InterruptedError("任务已取消")
            task["changed_files"] = workspace.commit(request.dirty_files)
            task["reply"], task["status"] = f"项目 {request.name} 已创建。", "done"
        except InterruptedError as error:
            task["status"], task["error"] = "cancelled", str(error)
        except Exception as error:
            task["status"], task["error"] = "error", str(error)
        finally:
            if workspace:
                workspace.close()
            _emit(task, task["status"], "模板创建结束", task["error"] or task["reply"])


@app.get("/api/health")
def health():
    return {"ok": True, "project": str(Runtime.project), "version": 1}


@app.get("/api/config")
def get_config():
    return {**_config(), "has_api_key": bool(credential("api_key"))}


@app.post("/api/config")
def set_config(request: ConfigRequest):
    if request.api_key:
        credential("api_key", request.api_key)
    config = request.model_dump(exclude={"api_key"})
    _save_config(config)
    return {**config, "has_api_key": bool(credential("api_key"))}


@app.post("/api/tasks")
def create_task(request: TaskRequest):
    root = _root(request.project_dir)
    task_id, task = _new_task(root)
    threading.Thread(target=_run_agent, args=(task, request), daemon=True).start()
    return {"task_id": task_id}


@app.post("/api/templates")
def create_template(request: TemplateRequest):
    if request.template not in TEMPLATES:
        raise HTTPException(400, "未知模板")
    root = _root(request.project_dir)
    task_id, task = _new_task(root)
    threading.Thread(target=_run_template, args=(task, request), daemon=True).start()
    return {"task_id": task_id}


@app.get("/api/templates")
def templates():
    return {"templates": [{"id": key, "name": cls().name, "description": cls().description}
                           for key, cls in TEMPLATES.items()]}


@app.get("/api/tasks/{task_id}")
def task_status(task_id: str, since: int = 0):
    task = Runtime.tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    with Runtime.mutex:
        return {k: v for k, v in task.items() if k not in ("events", "cancel")} | {
            "events": task["events"][max(0, since):], "next_sequence": len(task["events"])}


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    task = Runtime.tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    task["cancel"].set()
    return {"ok": True}


@app.post("/api/rollback")
def rollback_task(request: RollbackRequest):
    root = _root(request.project_dir)
    journal = Runtime.state / "transactions" / request.task_id / "journal.json"
    if not journal.is_file():
        raise HTTPException(404, "找不到任务快照")
    try:
        return {"changed_files": rollback(journal, root, request.dirty_files)}
    except Conflict as error:
        raise HTTPException(409, str(error)) from error


@app.get("/api/conversation")
def conversation(project_dir: str | None = None):
    root = _root(project_dir)
    return {"messages": ChatHistoryStore(str(_history_file(root))).get(str(root))}


@app.delete("/api/conversation")
def clear_conversation(project_dir: str | None = None):
    root = _root(project_dir)
    ChatHistoryStore(str(_history_file(root))).clear(str(root))
    return {"ok": True}


@app.get("/play/{play_token}/{file_path:path}")
def play_export(play_token: str, file_path: str = "index.html"):
    root = Runtime.play_tokens.get(play_token)
    if root is None:
        raise HTTPException(404, "试玩链接已失效")
    try:
        target = safe_path(root, file_path or "index.html")
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if not target.is_file():
        raise HTTPException(404, "导出文件不存在")
    return FileResponse(target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--port-file", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--godot-exe", default="")
    options = parser.parse_args()
    Runtime.project, Runtime.state = Path(options.project).resolve(), Path(options.state).resolve()
    Runtime.state.mkdir(parents=True, exist_ok=True)
    Runtime.token = Path(options.token_file).read_text(encoding="utf-8").strip()
    Path(options.token_file).unlink(missing_ok=True)
    Runtime.godot_exe = options.godot_exe
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2048)
    port = listener.getsockname()[1]
    Runtime.loopback_url = f"http://127.0.0.1:{port}"
    logging.basicConfig(filename=Runtime.state / "backend.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Path(options.port_file).write_text(str(port), encoding="ascii")
    import uvicorn
    uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False)).run(sockets=[listener])


if __name__ == "__main__":
    main()
