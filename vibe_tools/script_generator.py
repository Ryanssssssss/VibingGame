"""Godot .gd GDScript file generator.

Generates valid GDScript files with proper tab indentation and syntax.
"""

from __future__ import annotations

from vibe_tools.models import ScriptDesc, ExportVar, OnReadyVar, FunctionDesc


def _normalise_raw_code(code: str) -> str:
    """Normalise raw GDScript code coming from LLM output.

    * Converts leading spaces to tabs (4-space or 2-space → 1 tab per level).
    * Strips trailing whitespace from every line.
    * Ensures the file ends with exactly one newline.
    """
    out_lines: list[str] = []
    for line in code.split("\n"):
        stripped = line.rstrip()
        if not stripped:
            out_lines.append("")
            continue

        # Count leading whitespace and convert to tabs
        leading = ""
        rest = stripped
        # Already tab-indented — keep as-is
        if rest[0] == "\t":
            out_lines.append(stripped)
            continue
        # Space-indented — convert
        indent_chars = 0
        for ch in rest:
            if ch == " ":
                indent_chars += 1
            elif ch == "\t":
                indent_chars += 4  # treat a tab as 4 spaces
            else:
                break
        # Use 4-space = 1 tab; if author used 2-space, detect by checking if odd
        tab_size = 4
        if indent_chars > 0 and indent_chars < 4:
            tab_size = indent_chars  # likely 2-space style
        tab_count = indent_chars // tab_size if tab_size else 0
        out_lines.append("\t" * tab_count + rest.lstrip())

    # Remove excessive trailing blank lines
    while len(out_lines) > 1 and out_lines[-1] == "" and out_lines[-2] == "":
        out_lines.pop()

    result = "\n".join(out_lines)
    if not result.endswith("\n"):
        result += "\n"
    return result


def generate_script(desc: ScriptDesc) -> str:
    """Generate a .gd file from a ScriptDesc. Returns the file content."""
    # If raw_code is provided, normalise and return directly
    if desc.raw_code is not None:
        return _normalise_raw_code(desc.raw_code)

    lines: list[str] = []

    # Class name (optional)
    if desc.class_name:
        lines.append(f"class_name {desc.class_name}")

    # Extends
    lines.append(f"extends {desc.extends}")
    lines.append("")

    # Signals
    for sig in desc.signals:
        lines.append(f"signal {sig}")
    if desc.signals:
        lines.append("")

    # Export variables
    for ev in desc.exports:
        line = ""
        if ev.hint:
            line += f"{ev.hint}\n"
            line += f"var {ev.name}: {ev.type}"
        else:
            line += f"@export var {ev.name}: {ev.type}"
        if ev.default is not None:
            line += f" = {ev.default}"
        lines.append(line)
    if desc.exports:
        lines.append("")

    # @onready variables
    for ov in desc.onready_vars:
        line = "@onready var "
        if ov.type:
            line += f"{ov.name}: {ov.type} = {ov.node_path}"
        else:
            line += f"{ov.name} = {ov.node_path}"
        lines.append(line)
    if desc.onready_vars:
        lines.append("")

    # Functions
    for func in desc.functions:
        params_str = ", ".join(func.params) if func.params else ""
        lines.append(f"func {func.name}({params_str}):")

        # Process body — ensure tab indentation
        body_lines = func.body.split("\n")
        for bl in body_lines:
            if bl.strip():
                stripped = bl.lstrip("\t ")
                # Count existing indentation level
                indent_count = 0
                for ch in bl:
                    if ch == "\t":
                        indent_count += 1
                    elif ch == " ":
                        continue
                    else:
                        break
                indent = "\t" * max(1, indent_count)
                lines.append(f"{indent}{stripped}")
            else:
                lines.append("")

        lines.append("")
        lines.append("")

    content = "\n".join(lines)

    # Clean up trailing whitespace and ensure single final newline
    result_lines = [line.rstrip() for line in content.split("\n")]
    while len(result_lines) > 1 and result_lines[-1] == "" and result_lines[-2] == "":
        result_lines.pop()

    return "\n".join(result_lines) + "\n"
