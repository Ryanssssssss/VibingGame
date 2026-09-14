"""Versioned acceptance verdicts, independent of log verbosity."""
import re

RULE_VERSION = 1


def aggregate(result, process, logs, cancelled=False):
    diagnostics, defects = [], []
    observations = result.get("observations", [])
    normal_exit = process["status"] == "passed" and result.get("complete", False)
    # Only indented continuation lines belong to an error, never later load messages.
    blocks = []
    active = None
    for line in logs.splitlines():
        if line.startswith(("SCRIPT ERROR:", "ERROR:", "WARNING:")):
            blocks.append(line)
            active = len(blocks) - 1
        elif active is not None and line.startswith((" ", "\t")):
            blocks[active] += "\n" + line
        else:
            active = None
    for block in blocks:
        first = block.splitlines()[0]
        if first == "ERROR: Failed to read the root certificate store.":
            continue
        owner = re.search(r"res://[^\s)]+", block)
        cleanup = not owner and bool(re.fullmatch(r"(?:ERROR: \d+ resources still in use at exit|WARNING: \d+ ObjectDB instances were leaked at exit)(?: \(run with [`]?--verbose[`]? for details\))?\.", first))
        if cleanup and normal_exit:
            status, kind = "warning", "exit_cleanup"
        elif first.startswith("WARNING:"):
            status, kind = "warning", "runtime_warning"
        elif owner and owner.group().startswith("res://__vibe_test/"):
            status, kind = "blocked", "bridge"
        elif owner:
            status, kind = "failed", "game_script"
        else:
            status, kind = "blocked", "unknown_runtime"
        entry = {"status": status, "kind": kind, "summary": block}
        diagnostics.append(entry)
        if status == "failed":
            defects.append(entry)
    if not normal_exit and process["status"] != "cancelled":
        diagnostics.append({"status": "blocked", "kind": "execution", "summary": process.get("summary") or "测试执行记录不完整"})
    behavior, visual = "passed", "not_required"
    for item in observations:
        is_visual = item["step"]["op"] == "visual"
        if is_visual and "visual" not in item:
            item["visual"] = {"status": "blocked", "summary": "视觉步骤尚未完成判定或截图不可用"}
        state = item.get("visual", {}).get("status", "blocked") if is_visual else item.get("status", "blocked")
        if not is_visual and state == "failed" and item["step"]["op"] not in ("assert", "wait_until"):
            state = "blocked"
        if state == "failed" and (is_visual or item["step"]["op"] in ("assert", "wait_until")):
            defects.append({"kind": "visual" if is_visual else "assertion", "status": state,
                            "index": item.get("index"), "summary": item.get("visual", {}).get("summary") or item.get("summary") or "行为断言失败",
                            "observation": item})
        if is_visual:
            visual = "failed" if "failed" in (visual, state) else "blocked" if "blocked" in (visual, state) else state
        elif state != "passed":
            behavior = "failed" if "failed" in (behavior, state) else "blocked"
    if not result.get("complete"):
        behavior = "blocked" if behavior == "passed" else behavior
    if result.get("visual_required") and visual == "not_required":
        visual = "blocked"
        diagnostics.append({"status": "blocked", "kind": "visual", "summary": "必需的视觉步骤尚未执行"})
    if result.get("status") in ("failed", "blocked") and not defects:
        behavior = "blocked"
        diagnostics.append({"status": "blocked", "kind": "bridge", "summary": result.get("summary", "桥接执行失败")})
    blocked = [d for d in diagnostics if d["status"] == "blocked"]
    warnings = [d for d in diagnostics if d["status"] == "warning"]
    status = "failed" if defects else "blocked" if blocked or behavior == "blocked" or visual == "blocked" else "passed"
    if cancelled or process["status"] == "cancelled" or any(o.get("visual", {}).get("status") == "cancelled" for o in observations):
        status = "cancelled"
    reasons = [d["summary"] for d in defects + blocked]
    reasons += [o["visual"]["summary"] for o in observations if o.get("visual", {}).get("status") == "blocked"]
    result.update(rule_version=RULE_VERSION, behavior_status=behavior, visual_status=visual,
                  runtime_status="failed" if any(d["status"] == "failed" for d in diagnostics) else "blocked" if blocked else "warning" if warnings else "passed",
                  diagnostics=diagnostics, repairable_defects=defects, warning_count=len(warnings),
                  status=status, summary="任务已取消" if status == "cancelled" else "\n".join(reasons) or ("通过（有警告）" if warnings else "验收通过"), log_tail=logs[-12000:])
    return result
