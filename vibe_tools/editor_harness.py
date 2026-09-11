"""Project-bound acceptance harness used by the native Agent workspace."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from vibe_tools.godot_runner import GodotRunner

IGNORED = {".godot", ".git", ".hg", ".svn", "__pycache__", "node_modules", "_web_export", "_win_export"}


def project_fingerprint(project: Path) -> str:
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(project, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED)
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(project).as_posix()
            digest.update(relative.encode("utf-8"))
            try:
                stat = path.stat()
                digest.update(str(stat.st_size).encode("ascii"))
                digest.update(str(stat.st_mtime_ns).encode("ascii"))
            except OSError:
                digest.update(b"missing")
    return digest.hexdigest()


class AcceptanceHarness:
    def __init__(self, project: Path, godot_exe: str):
        self.project = project.resolve()
        self.agent_dir = self.project / ".godot" / "agent"
        self.plan_file = self.agent_dir / "acceptance_plan.json"
        self.artifact_root = self.agent_dir / "test_artifacts"
        self.runner = GodotRunner(godot_exe or None)

    def _default_plan(self) -> dict[str, Any]:
        return {
            "version": 1,
            "updated": time.time(),
            "verified_fingerprint": "",
            "cases": [{
                "id": uuid.uuid4().hex,
                "title": "项目结构与脚本可以通过验证",
                "given": "当前 Godot 项目已完成资源导入",
                "when": "运行项目结构和全部 GDScript 检查",
                "then": "没有结构错误或脚本解析错误",
                "enabled": True,
                "required": True,
                "requires_visual": False,
                "status": "pending",
                "failure": "",
                "artifacts": [],
            }],
        }

    def load_plan(self) -> dict[str, Any]:
        try:
            plan = json.loads(self.plan_file.read_text(encoding="utf-8"))
            if not isinstance(plan, dict) or not isinstance(plan.get("cases"), list):
                raise ValueError("invalid plan")
        except (OSError, ValueError):
            plan = self._default_plan()
        fingerprint = project_fingerprint(self.project)
        if plan.get("verified_fingerprint") and plan["verified_fingerprint"] != fingerprint:
            for case in plan["cases"]:
                if case.get("status") in ("passed", "failed"):
                    case["status"] = "stale"
                    case["failure"] = "项目在上次验证后发生修改，需要重新运行。"
        return plan

    def save_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        cases = []
        for value in plan.get("cases", []):
            if not isinstance(value, dict):
                continue
            cases.append({
                "id": str(value.get("id") or uuid.uuid4().hex),
                "title": str(value.get("title", "未命名验收目标"))[:200],
                "given": str(value.get("given", ""))[:4000],
                "when": str(value.get("when", ""))[:4000],
                "then": str(value.get("then", ""))[:4000],
                "enabled": bool(value.get("enabled", True)),
                "required": bool(value.get("required", False)),
                "requires_visual": bool(value.get("requires_visual", False)),
                "status": str(value.get("status", "pending")),
                "failure": str(value.get("failure", ""))[:8000],
                "artifacts": [str(item) for item in value.get("artifacts", []) if isinstance(item, str)],
            })
        saved = {
            "version": 1,
            "updated": time.time(),
            "verified_fingerprint": str(plan.get("verified_fingerprint", "")),
            "cases": cases,
        }
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.plan_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.plan_file)
        return saved

    def artifact(self, relative: str) -> Path:
        target = (self.artifact_root / relative).resolve()
        target.relative_to(self.artifact_root.resolve())
        if not target.is_file():
            raise FileNotFoundError(relative)
        return target

    def run(self, selected_ids: list[str], emit: Callable[[str, dict[str, Any]], None], cancel) -> dict[str, Any]:
        plan = self.load_plan()
        selected = set(selected_ids)
        cases = [case for case in plan["cases"] if case.get("enabled", True) and (not selected or case["id"] in selected)]
        run_id = uuid.uuid4().hex
        run_dir = self.artifact_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        emit("acceptance_plan", {"run_id": run_id, "plan": plan})
        passed = failed = skipped = 0
        for index, case in enumerate(cases):
            if cancel.is_set():
                break
            emit("harness_observation", {
                "run_id": run_id, "case_id": case["id"], "status": "running",
                "description": f"正在运行：{case['title']}", "index": index,
            })
            if case.get("requires_visual"):
                ok = False
                result = {"ok": False, "summary": "视觉判定不可用：当前验收运行器没有可用的视觉模型证据。"}
            else:
                result = self.runner.validate_all_scripts(str(self.project))
                ok = bool(result.get("ok"))
            case["status"] = "passed" if ok else "failed"
            case["failure"] = "" if ok else str(result.get("summary", "验收失败"))
            report_name = f"{case['id']}.json"
            report_path = run_dir / report_name
            report_path.write_text(json.dumps({
                "run_id": run_id, "case": case, "result": result, "created": time.time(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            case["artifacts"] = [f"{run_id}/{report_name}"]
            passed += int(ok)
            failed += int(not ok)
            emit("harness_result", {
                "run_id": run_id, "case_id": case["id"], "status": case["status"],
                "failure": case["failure"], "artifacts": case["artifacts"],
            })
        skipped = len(cases) - passed - failed
        if not cancel.is_set() and failed == 0 and cases:
            plan["verified_fingerprint"] = project_fingerprint(self.project)
        else:
            plan["verified_fingerprint"] = ""
        plan = self.save_plan(plan)
        summary = {"run_id": run_id, "passed": passed, "failed": failed, "skipped": skipped,
                   "cancelled": cancel.is_set(), "plan": plan}
        summary_path = run_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary
