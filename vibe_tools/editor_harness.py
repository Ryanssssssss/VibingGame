"""Versioned, project-bound acceptance plans and reproducible behavioral suites."""
from __future__ import annotations
from vibe_tools.editor_verdict import RULE_VERSION

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable

from vibe_tools.editor_behavior import BehaviorRunner, run_process
from vibe_tools.editor_test_plan import TestCase
from vibe_tools.editor_workspace import IGNORED as WORKSPACE_IGNORED, inventory
from vibe_tools.godot_runner import GodotRunner

IGNORED = WORKSPACE_IGNORED | {"_web_export", "_win_export", "__vibe_test"}


def project_fingerprint(project: Path) -> str:
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(project, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED)
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(project).as_posix()
            if relative == "chat_history.json":
                continue
            digest.update(relative.encode("utf-8") + b"\0")
            try:
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError:
                digest.update(b"missing")
    return digest.hexdigest()


def definition(case: dict) -> dict:
    return TestCase.model_validate(case).model_dump()


class AcceptanceHarness:
    def __init__(self, project: Path, godot_exe: str, vision=None, headless=False):
        self.project = project.resolve()
        self.agent_dir = self.project / ".godot" / "agent"
        self.plan_file = self.agent_dir / "acceptance_plan.json"
        self.artifact_root = self.agent_dir / "test_artifacts"
        self.runner = GodotRunner(godot_exe or None)
        self.behavior = BehaviorRunner(self.runner.exe_path if self.runner.is_available() else "", vision, headless)

    def _default_plan(self) -> dict:
        return {"version": 2, "updated": time.time(), "verified_fingerprint": "",
                "source_fingerprint": "", "task_id": "", "prompt": "", "repair_round": 0, "cases": []}

    def load_plan(self) -> dict:
        try:
            plan = json.loads(self.plan_file.read_text(encoding="utf-8"))
            if not isinstance(plan, dict) or not isinstance(plan.get("cases"), list):
                raise ValueError("invalid plan")
        except (OSError, ValueError):
            return self._default_plan()
        fingerprint = project_fingerprint(self.project)
        legacy = plan.get("version", 1) < 2
        plan["version"] = 2
        for case in plan["cases"]:
            if not case.get("steps"):
                case.update(status="needs_generation", failure="待生成可执行步骤", verified_fingerprint="")
            elif case.get("result") and case["result"].get("rule_version") != RULE_VERSION:
                case.update(status="stale", failure="旧判定规则，需复测", verified_fingerprint="")
                plan["verified_fingerprint"] = ""
            elif legacy or (case.get("verified_fingerprint") and case["verified_fingerprint"] != fingerprint):
                case.update(status="stale", failure="项目修改后需要重新运行")
        if legacy or plan.get("verified_fingerprint") != fingerprint:
            plan["verified_fingerprint"] = ""
        return plan

    def _persist(self, plan: dict) -> dict:
        plan["version"] = 2
        plan["updated"] = time.time()
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.plan_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.plan_file)
        return plan

    def save_plan(self, plan: dict) -> dict:
        """Public edits cannot forge server-owned verdicts or verification fingerprints."""
        previous = self.load_plan()
        old = {case["id"]: case for case in previous["cases"]}
        cases, ids = [], set()
        for raw in plan.get("cases", []):
            case = definition(raw)
            if case["id"] in ids:
                raise ValueError("测试 ID 重复")
            ids.add(case["id"])
            prior = old.get(case["id"])
            if prior and definition(prior) == case:
                case = prior
            else:
                case.update(status="pending" if case["steps"] else "needs_generation", failure="", artifacts=[], verified_fingerprint="")
            cases.append(case)
        previous.update(cases=cases, verified_fingerprint="")
        return self._persist(previous)

    def install_generated(self, cases: list[dict], task_id: str, prompt: str, context: dict) -> dict:
        plan = self.load_plan()
        manual = [case for case in plan["cases"] if not case.get("generated")]
        if plan["cases"]:
            archive = self.agent_dir / "plan_history"
            archive.mkdir(parents=True, exist_ok=True)
            (archive / (uuid.uuid4().hex + ".json")).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        plan = self.save_plan({"cases": manual + cases})
        plan.update(task_id=task_id, prompt=prompt, context=context, repair_round=0,
                    source_fingerprint=project_fingerprint(self.project))
        return self._persist(plan)

    def artifact(self, relative: str) -> Path:
        target = (self.artifact_root / relative).resolve()
        target.relative_to(self.artifact_root.resolve())
        if not target.is_file():
            raise FileNotFoundError(relative)
        return target

    def apply_reviews(self, reviews: dict, task_id: str, revision: int) -> dict:
        """Preserve the old plan and reports before changing test assumptions."""
        plan = self.load_plan()
        archive = self.agent_dir / "plan_history"
        archive.mkdir(parents=True, exist_ok=True)
        (archive / (uuid.uuid4().hex + ".json")).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        for index, case in enumerate(plan["cases"]):
            review = reviews.get(case["id"])
            if review is None:
                continue
            if review["decision"] == "revise":
                case = {**review["replacement"], "status": "pending", "failure": "测试方案已修订，等待复测",
                        "verified_fingerprint": "", "artifacts": []}
                plan["cases"][index] = case
            elif review["decision"] == "blocked":
                case.update(status="blocked", failure="测试合理性审查受阻：" + review["reason"], verified_fingerprint="")
            case["test_review"] = review
        plan.setdefault("review_history", []).append({"task_id": task_id, "revision": revision, "reviews": reviews})
        plan.update(plan_revision=revision, verified_fingerprint="")
        return self._persist(plan)

    def record_review_summary(self, summary: dict) -> None:
        path = self.artifact(summary["attempt_id"] + "/summary.json")
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    def _preflight(self, project: Path, output: Path, cancel) -> dict:
        if not self.behavior.exe:
            return {"status": "blocked", "summary": "Godot 可执行文件不可用"}
        errors = self.runner._structural_validate(project)
        if errors:
            return {"status": "failed", "summary": "\n".join(errors),
                    "repairable_defects": [{"kind": "structure", "summary": error} for error in errors]}
        imported = run_process([self.behavior.exe, "--headless", "--editor", "--import"], project, output / "import.log", 60, cancel)
        if imported["status"] != "passed":
            return {**imported, "status": "cancelled" if imported["status"] == "cancelled" else "blocked"}
        for index, script in enumerate(sorted(project.rglob("*.gd"))):
            if any(part in IGNORED for part in script.relative_to(project).parts):
                continue
            path = "res://" + script.relative_to(project).as_posix()
            log = output / f"script_{index}.log"
            checked = run_process([self.behavior.exe, "--headless", "--script", path, "--check-only"], project, log, 30, cancel)
            content = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            if checked["status"] != "passed" or "SCRIPT ERROR:" in content:
                defect = "SCRIPT ERROR:" in content
                detail = path + "\n" + checked["summary"] + "\n" + content[-8000:]
                return {"status": "cancelled" if checked["status"] == "cancelled" else "failed" if defect else "blocked",
                        "summary": detail, "repairable_defects": [{"kind": "game_script", "summary": detail}] if defect else []}
        return {"status": "passed", "summary": "项目结构及脚本检查通过"}

    def run(self, selected_ids: list[str], emit: Callable, cancel, *, task_id="", repair_round=0) -> dict:
        plan = self.load_plan()
        selected = set(selected_ids)
        if selected - {case["id"] for case in plan["cases"]}:
            raise ValueError("选中的测试不存在")
        cases = [case for case in plan["cases"] if case.get("enabled", True) and (not selected or case["id"] in selected)]
        attempt_id = uuid.uuid4().hex
        run_dir = self.artifact_root / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = project_fingerprint(self.project)
        plan.update(repair_round=repair_round, verified_fingerprint="")
        emit("acceptance_plan", {"plan": plan})
        counts = {"passed": 0, "failed": 0, "blocked": 0, "cancelled": 0}
        completed_ids = set()
        with tempfile.TemporaryDirectory(prefix="vibe-suite-") as directory:
            snapshot = Path(directory) / "project"
            inventory(self.project)
            shutil.copytree(self.project, snapshot, ignore=shutil.ignore_patterns(*IGNORED))
            if project_fingerprint(snapshot) != fingerprint:
                raise ValueError("建立测试快照时项目发生修改，请重新运行")
            emit("harness_phase", {"phase": "preflight", "description": "检查项目结构和脚本", "repair_round": repair_round})
            preflight = self._preflight(snapshot, run_dir, cancel) if cases else {"status": "blocked", "summary": "没有启用的测试"}
            (run_dir / "preflight.json").write_text(json.dumps(preflight, ensure_ascii=False, indent=2), encoding="utf-8")
            for index, case in enumerate(cases):
                if cancel.is_set():
                    break
                emit("harness_observation", {"case_id": case["id"], "status": "running",
                     "description": f"[{index + 1}/{len(cases)}] {case['title']}", "index": index, "repair_round": repair_round})
                case_dir = run_dir / case["id"]
                case_dir.mkdir()
                try:
                    result = (self.behavior.run_case(snapshot, case, case_dir, cancel) if preflight["status"] == "passed"
                              else {"status": "blocked", "summary": "前置检查未通过，行为测试未执行", "preflight": preflight})
                except (ValueError, OSError) as error:
                    result = {"status": "blocked", "summary": str(error)}
                result.setdefault("rule_version", RULE_VERSION)
                case.update(status=result["status"], failure=result.get("summary", ""),
                            result=result, verified_fingerprint=fingerprint, repair_round=repair_round)
                counts[result["status"]] += 1
                completed_ids.add(case["id"])
                report_path = case_dir / "report.json"
                report_path.write_text(json.dumps({"task_id": task_id, "attempt_id": attempt_id, "case": case,
                    "result": result, "created": time.time()}, ensure_ascii=False, indent=2), encoding="utf-8")
                artifacts = [report_path] + sorted(case_dir.glob("*.png")) + sorted(case_dir.glob("*.log"))
                case["artifacts"] = [p.relative_to(self.artifact_root).as_posix() for p in artifacts]
                self._persist(plan)
                emit("harness_result", {"case_id": case["id"], "status": case["status"], "failure": case["failure"],
                     "artifacts": case["artifacts"], "result": result})
        unchanged = fingerprint == project_fingerprint(self.project)
        for case in cases:
            if case["id"] not in completed_ids:
                case.update(status="cancelled", failure="任务已取消", verified_fingerprint="")
        enabled = [case for case in plan["cases"] if case.get("enabled", True)]
        all_passed = bool(enabled) and all(case.get("status") == "passed" and case.get("verified_fingerprint") == fingerprint for case in enabled)
        if unchanged and not cancel.is_set() and all_passed and preflight["status"] == "passed":
            plan["verified_fingerprint"] = fingerprint
        if not unchanged:
            for case in cases:
                case.update(status="stale", failure="测试期间项目发生修改，请重新运行")
        plan = self._persist(plan)
        summary = {"warning_count": sum(c.get("result", {}).get("warning_count", 0) for c in cases), "attempt_id": attempt_id, **counts, "skipped": len(cases) - sum(counts.values()),
                   "cancelled": cancel.is_set(), "stale": not unchanged, "preflight": preflight,
                   "acceptance_ok": bool(plan["verified_fingerprint"]), "repair_round": repair_round, "plan": plan}
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        emit("acceptance_plan", {"plan": plan})
        return summary
