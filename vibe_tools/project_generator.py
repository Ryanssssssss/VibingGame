"""Project generator - orchestrates scene, script, and project.godot generation.

Writes files atomically (temp → rename) where possible so a crash mid-generation
doesn't leave a half-written project.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from vibe_tools.models import ProjectPlan, ProjectSettings, InputEvent
from vibe_tools.scene_generator import generate_scene
from vibe_tools.script_generator import generate_script

logger = logging.getLogger(__name__)


# ─── project.godot generation ───

def _generate_project_godot(settings: ProjectSettings) -> str:
    """Generate the project.godot file content."""
    lines: list[str] = []

    # Header
    lines.append("; Engine configuration file.")
    lines.append("; It's best edited using the editor UI and not directly,")
    lines.append("; since the parameters that go here are not all obvious.")
    lines.append(";")
    lines.append("; Format:")
    lines.append(";   [section] ; section goes between []")
    lines.append(";   param=value ; assign values to parameters")
    lines.append("")

    # Config version (Godot 4.x = 5)
    lines.append("config_version=5")
    lines.append("")

    # [application]
    lines.append("[application]")
    lines.append("")
    lines.append(f'config/name="{settings.name}"')
    lines.append(f'run/main_scene="{settings.main_scene}"')
    lines.append('config/features=PackedStringArray("4.4")')
    lines.append("")

    # [display]
    lines.append("[display]")
    lines.append("")
    lines.append(f"window/size/viewport_width={settings.window_width}")
    lines.append(f"window/size/viewport_height={settings.window_height}")
    if settings.stretch_mode != "disabled":
        lines.append(f'window/stretch/mode="{settings.stretch_mode}"')
    if settings.stretch_aspect != "ignore":
        lines.append(f'window/stretch/aspect="{settings.stretch_aspect}"')
    lines.append("")

    # [input] - action mappings
    if settings.input_actions:
        lines.append("[input]")
        lines.append("")
        for action_name, events in settings.input_actions.items():
            event_strs = [_format_input_event(evt) for evt in events]
            events_array = ", ".join(event_strs)
            lines.append(f'{action_name}={{')
            lines.append(f'"deadzone": 0.5,')
            lines.append(f'"events": [{events_array}]')
            lines.append(f'}}')
        lines.append("")

    # [rendering]
    lines.append("[rendering]")
    lines.append("")
    lines.append('renderer/rendering_method="gl_compatibility"')
    lines.append("")

    # Custom sections
    for section, params in settings.custom.items():
        lines.append(f"[{section}]")
        lines.append("")
        for key, value in params.items():
            if isinstance(value, str):
                lines.append(f'{key}="{value}"')
            elif isinstance(value, bool):
                lines.append(f"{key}={str(value).lower()}")
            else:
                lines.append(f"{key}={value}")
        lines.append("")

    return "\n".join(lines) + "\n"


def _format_input_event(evt: InputEvent) -> str:
    """Format an InputEvent for project.godot."""
    if evt.type == "key":
        return (
            f'Object(InputEventKey,"resource_local_to_scene":false,"resource_name":"",'
            f'"device":0,"window_id":0,"alt_pressed":false,"shift_pressed":false,'
            f'"ctrl_pressed":false,"meta_pressed":false,"pressed":false,"keycode":0,'
            f'"physical_keycode":{evt.value},"key_label":0,"unicode":0,"location":0,'
            f'"echo":false,"script":null)'
        )
    elif evt.type == "joypad_button":
        return (
            f'Object(InputEventJoypadButton,"resource_local_to_scene":false,'
            f'"resource_name":"","device":-1,"button_index":{evt.value},'
            f'"pressure":0.0,"pressed":false,"script":null)'
        )
    elif evt.type == "joypad_motion":
        parts = evt.value.split(",")
        axis = parts[0] if len(parts) > 0 else "0"
        axis_value = parts[1] if len(parts) > 1 else "1.0"
        return (
            f'Object(InputEventJoypadMotion,"resource_local_to_scene":false,'
            f'"resource_name":"","device":-1,"axis":{axis},'
            f'"axis_value":{axis_value},"script":null)'
        )
    return ""


# ─── Project Generator ───

class ProjectGenerator:
    """Generates a complete Godot project from a ProjectPlan."""

    def generate(self, plan: ProjectPlan) -> str:
        """Generate all project files and return the output directory path.

        Raises:
            OSError: If directory creation or file writing fails.
            ValueError: If the plan contains invalid data.
        """
        output_dir = Path(plan.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        generated_files: list[str] = []

        try:
            # Generate project.godot
            plan.settings.name = plan.name
            project_godot = _generate_project_godot(plan.settings)
            self._write_file(output_dir / "project.godot", project_godot)
            generated_files.append("project.godot")

            # Generate scene files
            for scene in plan.scenes:
                scene_content = generate_scene(scene)
                scene_path = output_dir / scene.filename
                scene_path.parent.mkdir(parents=True, exist_ok=True)
                self._write_file(scene_path, scene_content)
                generated_files.append(scene.filename)

            # Generate script files
            for script in plan.scripts:
                script_content = generate_script(script)
                script_path = output_dir / script.filename
                script_path.parent.mkdir(parents=True, exist_ok=True)
                self._write_file(script_path, script_content)
                generated_files.append(script.filename)

            # Create default icon if not exists
            icon_path = output_dir / "icon.svg"
            if not icon_path.exists():
                self._write_file(icon_path, _DEFAULT_ICON_SVG)
                generated_files.append("icon.svg")

        except Exception:
            logger.error(
                "Project generation partially failed after writing: %s",
                generated_files,
                exc_info=True,
            )
            raise

        logger.info(
            "Generated project '%s' with %d file(s) at %s",
            plan.name, len(generated_files), output_dir,
        )
        return str(output_dir)

    @staticmethod
    def _write_file(path: Path, content: str) -> None:
        """Write *content* to *path* atomically (write-tmp → rename).

        Falls back to a direct write if atomic rename is not possible
        (e.g. cross-device).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".vibe_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    f.write(content)
                os.replace(tmp_path, str(path))
            except BaseException:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError:
            # Fallback: direct write (e.g. on some Windows network drives)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)


_DEFAULT_ICON_SVG = """<svg height="128" width="128" xmlns="http://www.w3.org/2000/svg">
  <rect x="2" y="2" width="124" height="124" rx="14" fill="#363d52" stroke="#212532" stroke-width="4"/>
  <g transform="translate(32,32)">
    <path d="M32 0 L64 56 L0 56 Z" fill="#478cbf"/>
  </g>
</svg>
"""
