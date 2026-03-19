"""Godot editor integration - launch, validate, and manage the Godot editor.

Handles auto-detection of the Godot executable, project validation,
GDScript syntax checking, headless project execution, and Web export.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

logger = logging.getLogger(__name__)

# Flag constant for hiding console windows on Windows
_CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED_PROCESS: int = getattr(subprocess, "DETACHED_PROCESS", 0)


class GodotRunner:
    """Manages interaction with the Godot editor executable."""

    # Timeout (seconds) for individual GDScript checks
    SCRIPT_CHECK_TIMEOUT: int = 30

    def __init__(self, godot_exe_path: str | None = None) -> None:
        if godot_exe_path and Path(godot_exe_path).exists():
            self._exe = Path(godot_exe_path)
        else:
            self._exe = self._auto_detect()

    # ─── Executable detection ───

    @staticmethod
    def _auto_detect() -> Path:
        """Auto-detect the Godot executable in bin/ directory, GODOT_EXE env var, or PATH."""
        engine_root = Path(__file__).resolve().parent.parent

        # 1. Check compiled binaries in the engine bin/ directory
        candidates = [
            engine_root / "bin" / "godot.windows.editor.x86_64.exe",
            engine_root / "bin" / "godot.windows.editor.x86_64.console.exe",
            engine_root / "bin" / "godot.linuxbsd.editor.x86_64",
            engine_root / "bin" / "godot.macos.editor.arm64",
            engine_root / "bin" / "godot.macos.editor.x86_64",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate

        # 2. Check GODOT_EXE environment variable
        env_exe = os.environ.get("GODOT_EXE")
        if env_exe and Path(env_exe).exists():
            return Path(env_exe)

        # 3. Check PATH
        godot_in_path = shutil.which("godot")
        if godot_in_path:
            return Path(godot_in_path)

        raise FileNotFoundError(
            "Could not find Godot executable. "
            "Set GODOT_EXE environment variable, configure the path in Settings, "
            "or add Godot to your PATH."
        )

    @property
    def exe_path(self) -> str:
        return str(self._exe)

    def is_available(self) -> bool:
        """Check if the Godot executable exists."""
        return self._exe.exists()

    # ─── Launch helpers ───

    def open_editor(self, project_dir: str) -> subprocess.Popen | None:
        """Open a project in the Godot editor."""
        if not self.is_available():
            return None
        project_path = Path(project_dir)
        if not (project_path / "project.godot").exists():
            logger.warning("Cannot open editor: project.godot not found in %s", project_dir)
            return None
        try:
            proc = subprocess.Popen(
                [str(self._exe), "--path", str(project_path), "--editor"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_DETACHED_PROCESS if sys.platform == "win32" else 0,
            )
            return proc
        except Exception:
            logger.error("Failed to open Godot editor for %s", project_dir, exc_info=True)
            return None

    def import_resources(self, project_dir: str, timeout: int = 60) -> bool:
        """Run ``--headless --import`` so Godot converts assets (GLB, images …).

        Returns True on success, False on failure.  This is idempotent –
        calling it when the ``.godot/imported/`` cache already exists is a
        fast no-op inside Godot.
        """
        if not self.is_available():
            return False
        project_path = Path(project_dir)
        if not (project_path / "project.godot").exists():
            return False
        try:
            result = subprocess.run(
                [str(self._exe), "--path", str(project_path), "--headless", "--import"],
                capture_output=True,
                timeout=timeout,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if result.returncode != 0:
                logger.warning(
                    "Godot --import returned %d for %s:\n%s",
                    result.returncode, project_dir,
                    result.stderr.decode("utf-8", errors="replace")[-2000:],
                )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            logger.warning("Godot --import timed out for %s", project_dir)
            return False
        except Exception:
            logger.error("Failed to import resources for %s", project_dir, exc_info=True)
            return False

    def run_project(self, project_dir: str) -> subprocess.Popen | None:
        """Run a project directly (game mode, not editor).

        Automatically runs ``--import`` first to ensure assets like GLB
        models are available in the ``.godot/imported/`` cache.
        """
        if not self.is_available():
            return None
        project_path = Path(project_dir)
        if not (project_path / "project.godot").exists():
            logger.warning("Cannot run project: project.godot not found in %s", project_dir)
            return None

        # Ensure resources are imported (GLB, textures, etc.)
        self.import_resources(project_dir)

        try:
            proc = subprocess.Popen(
                [str(self._exe), "--path", str(project_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_DETACHED_PROCESS if sys.platform == "win32" else 0,
            )
            return proc
        except Exception:
            logger.error("Failed to run project %s", project_dir, exc_info=True)
            return None

    # ─── Validation ───

    def validate_project(self, project_dir: str) -> bool:
        """Basic validation of project structure. Returns True if no issues found."""
        return len(self._structural_validate(project_dir)) == 0

    def check_gdscript(self, project_dir: str, script_path: str) -> dict:
        """Check a GDScript file for errors using Godot's --check-only mode.

        Args:
            project_dir: Path to the project root (containing project.godot)
            script_path: res:// path to the script (e.g. "res://main.gd")

        Returns:
            dict with 'ok' (bool) and 'errors' (list of error strings)
        """
        if not self.is_available():
            return {"ok": False, "errors": ["Godot executable not found"]}

        project_path = Path(project_dir)
        if not (project_path / "project.godot").exists():
            return {"ok": False, "errors": ["No project.godot found"]}

        try:
            result = subprocess.run(
                [str(self._exe), "--path", str(project_path), "--headless",
                 "--script", script_path, "--check-only"],
                capture_output=True,
                timeout=self.SCRIPT_CHECK_TIMEOUT,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )

            # Decode as UTF-8 with fallback to avoid GBK errors on Windows
            stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            combined = stdout + "\n" + stderr
            errors: list[str] = []

            error_keywords = {"error", "parse error", "cannot", "invalid", "unexpected"}
            for line in combined.split("\n"):
                line = line.strip()
                if not line:
                    continue
                low = line.lower()
                if any(kw in low for kw in error_keywords):
                    clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
                    if clean.strip():
                        errors.append(clean.strip())

            if result.returncode != 0 and not errors:
                errors.append(f"Script check failed with exit code {result.returncode}")

            return {"ok": result.returncode == 0 and len(errors) == 0, "errors": errors}
        except subprocess.TimeoutExpired:
            return {"ok": False, "errors": [f"Script check timed out after {self.SCRIPT_CHECK_TIMEOUT}s"]}
        except Exception as e:
            return {"ok": False, "errors": [f"Script check error: {e}"]}

    def validate_all_scripts(self, project_dir: str) -> dict:
        """Validate all GDScript files in a project.

        Returns:
            dict with 'ok', 'results', 'structural_errors', and 'summary'.
        """
        project_path = Path(project_dir)
        results: dict[str, dict] = {}
        all_ok = True

        for gd_file in sorted(project_path.rglob("*.gd")):
            rel_path = str(gd_file.relative_to(project_path)).replace("\\", "/")
            res_path = f"res://{rel_path}"
            check_result = self.check_gdscript(project_dir, res_path)
            results[rel_path] = check_result
            if not check_result["ok"]:
                all_ok = False

        struct_errors = self._structural_validate(project_dir)
        if struct_errors:
            all_ok = False

        summary_parts: list[str] = []
        if struct_errors:
            summary_parts.append(f"Structural issues: {'; '.join(struct_errors)}")
        for script, result in results.items():
            if not result["ok"]:
                summary_parts.append(f"{script}: {'; '.join(result['errors'])}")

        return {
            "ok": all_ok,
            "results": results,
            "structural_errors": struct_errors,
            "summary": "\n".join(summary_parts) if summary_parts else "All scripts passed validation.",
        }

    def _structural_validate(self, project_dir: str) -> list[str]:
        """Structural validation of a project (missing files, bad references, shapes, etc.)."""
        project_path = Path(project_dir)
        issues: list[str] = []

        project_godot = project_path / "project.godot"
        if not project_godot.exists():
            issues.append("Missing project.godot")
            return issues

        content = project_godot.read_text(encoding="utf-8")

        # Check main scene exists
        for line in content.split("\n"):
            if "run/main_scene" in line:
                scene_path = line.split("=", 1)[1].strip().strip('"')
                local_path = scene_path.replace("res://", "")
                if not (project_path / local_path).exists():
                    issues.append(f"Main scene not found: {local_path}")
                break

        # Validate each .tscn scene
        for tscn_file in project_path.rglob("*.tscn"):
            tscn_content = tscn_file.read_text(encoding="utf-8")
            rel = str(tscn_file.relative_to(project_path))
            lines = tscn_content.split("\n")

            # Check referenced scripts exist
            for line in lines:
                if 'type="Script"' in line and 'path="' in line:
                    try:
                        path_start = line.index('path="') + 6
                        path_end = line.index('"', path_start)
                        script_path_str = line[path_start:path_end].replace("res://", "")
                        if not (project_path / script_path_str).exists():
                            issues.append(f"{rel}: Missing script reference {script_path_str}")
                    except ValueError:
                        pass

            # Check for invalid node types
            for line in lines:
                if line.startswith("[node "):
                    type_match = re.search(r'type="(\w+)"', line)
                    if type_match and type_match.group(1) == "PackedScene":
                        name_match = re.search(r'name="(\w+)"', line)
                        node_name = name_match.group(1) if name_match else "unknown"
                        issues.append(
                            f"{rel}: Node '{node_name}' uses invalid type 'PackedScene' "
                            f"- should use instance=ExtResource() syntax"
                        )

            # Check CollisionShape nodes without a shape assigned
            collision_nodes: set[str] = set()
            collision_with_shape: set[str] = set()
            current_node_name: str | None = None
            for line in lines:
                if line.startswith("[node "):
                    name_match = re.search(r'name="([^"]+)"', line)
                    type_match = re.search(r'type="([^"]+)"', line)
                    if name_match and type_match:
                        current_node_name = name_match.group(1)
                        if type_match.group(1) in ("CollisionShape2D", "CollisionShape3D"):
                            collision_nodes.add(current_node_name)
                elif current_node_name and line.startswith("shape = "):
                    if current_node_name in collision_nodes:
                        collision_with_shape.add(current_node_name)

            for node_name in collision_nodes - collision_with_shape:
                issues.append(
                    f"{rel}: CollisionShape '{node_name}' has no shape — "
                    f"must assign a SubResource (e.g. RectangleShape2D)"
                )

            # Check unquoted text values
            for i, line in enumerate(lines, 1):
                if line.startswith("text = ") and not line.startswith("text = \""):
                    val = line.split("=", 1)[1].strip()
                    if val and not val.replace(".", "").replace("-", "").isdigit():
                        if not val.startswith(("SubResource", "ExtResource")):
                            issues.append(f'{rel} line {i}: text value should be quoted: text = "{val}"')

        return issues

    # ─── Web Export ───

    # Default export timeout (seconds) – exporting can be slow for large projects
    EXPORT_TIMEOUT: int = 120

    @staticmethod
    def _web_export_templates_dir() -> Path:
        """Return the OS-specific Godot export templates directory."""
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
            return base / "Godot" / "export_templates"
        elif sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "Godot" / "export_templates"
        else:
            return Path.home() / ".local" / "share" / "godot" / "export_templates"

    def get_web_template_status(self) -> dict:
        """Check if Web export templates are installed.

        Returns dict with 'installed' (bool), 'path', 'version', and 'hint'.
        Prefers stable template versions over dev versions.
        """
        tpl_dir = self._web_export_templates_dir()
        web_files = ("web_release.zip", "web_debug.zip",
                     "web_nothreads_release.zip", "web_nothreads_debug.zip",
                     "godot.web.template_release.wasm32.zip",
                     "godot.web.template_debug.wasm32.zip")
        found: list[tuple[str, str]] = []  # (version_name, path)
        if tpl_dir.exists():
            for version_dir in tpl_dir.iterdir():
                if version_dir.is_dir():
                    for wf in web_files:
                        if (version_dir / wf).exists():
                            found.append((version_dir.name, str(version_dir)))
                            break
        if found:
            # Prefer stable versions (e.g. "4.6.1.stable") over dev
            stable = [(v, p) for v, p in found if "stable" in v]
            choice = stable[0] if stable else found[0]
            return {
                "installed": True,
                "path": choice[1],
                "version": choice[0],
                "hint": None,
            }
        return {
            "installed": False,
            "path": str(tpl_dir),
            "version": None,
            "hint": (
                "Web export templates not found. To install:\n"
                "1. Open Godot Editor → Editor → Manage Export Templates → Download\n"
                "   OR\n"
                "2. Download from https://godotengine.org/download and extract to:\n"
                f"   {tpl_dir}/<version>/"
            ),
        }

    def _find_stable_exe_for_export(self) -> Path | None:
        """Find a stable Godot exe matching the installed export template version.

        When the main exe is a dev build (e.g. 4.7.dev) but templates are from
        a stable release (e.g. 4.6.1.stable), the wasm and pck are incompatible.
        We look for a matching stable binary in bin/ to use for export only.
        """
        tpl_status = self.get_web_template_status()
        if not tpl_status["installed"] or not tpl_status.get("version"):
            return None

        tpl_version = tpl_status["version"]  # e.g. "4.6.1.stable"

        # If main exe version matches templates, no need for a separate binary
        # Check by running --version, but simpler: if "stable" is in tpl_version
        # and our exe path contains "dev" or version.py says dev, use stable exe.
        engine_root = Path(__file__).resolve().parent.parent
        stable_candidates = [
            engine_root / "bin" / f"godot-{tpl_version}.exe",
            engine_root / "bin" / f"godot-{tpl_version.replace('.stable', '')}-stable.exe",
        ]
        # Also try generic stable patterns
        version_short = tpl_version.replace(".stable", "")  # "4.6.1"
        stable_candidates.extend([
            engine_root / "bin" / f"godot-{version_short}-stable.exe",
            engine_root / "bin" / f"Godot_v{tpl_version}_win64.exe",
            engine_root / "bin" / f"Godot_v{version_short}-stable_win64.exe",
        ])

        for candidate in stable_candidates:
            if candidate.exists():
                logger.info("Using stable Godot for web export: %s", candidate)
                return candidate

        return None

    @staticmethod
    def _write_export_presets(project_dir: str) -> Path:
        """Write an export_presets.cfg for Web export if one doesn't exist."""
        project_path = Path(project_dir)
        presets_path = project_path / "export_presets.cfg"

        if presets_path.exists():
            content = presets_path.read_text(encoding="utf-8")
            if "platform=\"Web\"" in content or 'platform="Web"' in content:
                return presets_path

        cfg = textwrap.dedent("""\
            [preset.0]

            name="Web"
            platform="Web"
            runnable=true
            dedicated_server=false
            custom_features=""
            export_filter="all_resources"
            include_filter="*.png,*.jpg,*.jpeg,*.webp,*.bmp,*.tga,*.svg,*.wav,*.ogg,*.mp3,*.tres,*.ttf,*.otf,*.glb,*.gltf,*.obj"
            exclude_filter=""

            [preset.0.options]

            custom_template/debug=""
            custom_template/release=""
            variant/extensions_support=false
            vram_texture_compression/for_desktop=true
            vram_texture_compression/for_mobile=false
            html/export_icon=true
            html/custom_html_shell=""
            html/head_include=""
            html/canvas_resize_policy=2
            html/focus_canvas_on_start=true
            html/experimental_virtual_keyboard=false
            progressive_web_app/enabled=false
        """)
        presets_path.write_text(cfg, encoding="utf-8")
        return presets_path

    def export_project_web(self, project_dir: str, output_dir: str | None = None) -> dict:
        """Export a Godot project as HTML5/Web.

        Uses a stable Godot binary matching the export template version when
        available, to avoid version mismatches between dev builds and templates.

        Args:
            project_dir: Path to the project root (containing project.godot)
            output_dir: Where to place exported files. Defaults to project_dir/_web_export/

        Returns:
            dict with 'ok', 'export_dir', 'index_html', 'files', 'error'
        """
        if not self.is_available():
            return {"ok": False, "error": "Godot executable not found"}

        project_path = Path(project_dir).resolve()
        if not (project_path / "project.godot").exists():
            return {"ok": False, "error": "No project.godot found in project directory"}

        tpl_status = self.get_web_template_status()
        if not tpl_status["installed"]:
            return {"ok": False, "error": tpl_status["hint"]}

        # Prefer a stable exe matching the template version for export
        export_exe = self._find_stable_exe_for_export() or self._exe

        if output_dir:
            export_dir = Path(output_dir).resolve()
        else:
            export_dir = project_path / "_web_export"

        export_dir.mkdir(parents=True, exist_ok=True)
        index_path = export_dir / "index.html"

        self._write_export_presets(project_dir)

        # ── Pre-export: force Godot to import all resources ──
        # When assets are uploaded via API (not through the editor), the
        # .godot/imported/ cache may be missing or incomplete. Without this
        # step, the exported .pck will NOT contain those assets.
        try:
            import_result = subprocess.run(
                [
                    str(export_exe), "--path", str(project_path),
                    "--headless", "--import",
                ],
                capture_output=True,
                timeout=60,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            logger.info("Resource import exit code: %d", import_result.returncode)
        except subprocess.TimeoutExpired:
            logger.warning("Resource import timed out (60s), proceeding with export anyway")
        except Exception as e:
            logger.warning("Resource import failed: %s, proceeding with export anyway", e)

        try:
            result = subprocess.run(
                [
                    str(export_exe), "--path", str(project_path),
                    "--headless", "--export-release", "Web",
                    str(index_path),
                ],
                capture_output=True,
                timeout=self.EXPORT_TIMEOUT,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )

            # Decode as UTF-8 with fallback to avoid GBK errors on Windows
            stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            combined = stdout + "\n" + stderr

            if not index_path.exists():
                error_lines = [
                    line.strip() for line in combined.splitlines()
                    if line.strip() and any(kw in line.lower() for kw in
                                            ["error", "failed", "cannot", "invalid"])
                ]
                error_msg = "\n".join(error_lines[:10]) if error_lines else combined[-1000:]
                return {
                    "ok": False,
                    "error": f"Export failed (exit code {result.returncode}):\n{error_msg}",
                }

            exported_files = []
            for f in sorted(export_dir.rglob("*")):
                if f.is_file():
                    exported_files.append({
                        "name": f.name,
                        "path": str(f.relative_to(export_dir)),
                        "size": f.stat().st_size,
                    })

            return {
                "ok": True,
                "export_dir": str(export_dir),
                "index_html": str(index_path),
                "files": exported_files,
                "message": f"Web export successful. {len(exported_files)} files generated.",
            }

        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"Export timed out after {self.EXPORT_TIMEOUT}s"}
        except Exception as e:
            return {"ok": False, "error": f"Export error: {e}"}

    # ─── Windows Export ───

    def get_windows_template_status(self) -> dict:
        """Check if Windows Desktop export templates are installed.

        Returns dict with 'installed' (bool), 'path', 'version', and 'hint'.
        """
        tpl_dir = self._web_export_templates_dir()  # same parent dir for all templates
        win_files = (
            "windows_release_x86_64.exe",
            "windows_debug_x86_64.exe",
            "godot.windows.template_release.x86_64.exe",
            "godot.windows.template_debug.x86_64.exe",
        )
        found: list[tuple[str, str]] = []
        if tpl_dir.exists():
            for version_dir in tpl_dir.iterdir():
                if version_dir.is_dir():
                    for wf in win_files:
                        if (version_dir / wf).exists():
                            found.append((version_dir.name, str(version_dir)))
                            break
        if found:
            stable = [(v, p) for v, p in found if "stable" in v]
            choice = stable[0] if stable else found[0]
            return {
                "installed": True,
                "path": choice[1],
                "version": choice[0],
                "hint": None,
            }
        return {
            "installed": False,
            "path": str(tpl_dir),
            "version": None,
            "hint": (
                "Windows export templates not found. To install:\n"
                "1. Open Godot Editor → Editor → Manage Export Templates → Download\n"
                "   OR\n"
                "2. Download from https://godotengine.org/download and extract to:\n"
                f"   {tpl_dir}/<version>/"
            ),
        }

    @staticmethod
    def _write_windows_export_presets(project_dir: str) -> Path:
        """Ensure export_presets.cfg contains a Windows Desktop preset."""
        project_path = Path(project_dir)
        presets_path = project_path / "export_presets.cfg"

        if presets_path.exists():
            content = presets_path.read_text(encoding="utf-8")
            if 'platform="Windows Desktop"' in content:
                return presets_path
            # Append a Windows preset after existing presets
            # Find the next available preset index
            import re as _re
            indices = [int(m.group(1)) for m in _re.finditer(r'\[preset\.(\d+)\]', content)]
            next_idx = max(indices) + 1 if indices else 0
        else:
            content = ""
            next_idx = 0

        win_cfg = textwrap.dedent(f"""\

            [preset.{next_idx}]

            name="Windows Desktop"
            platform="Windows Desktop"
            runnable=true
            dedicated_server=false
            custom_features=""
            export_filter="all_resources"
            include_filter="*.png,*.jpg,*.jpeg,*.webp,*.bmp,*.tga,*.svg,*.wav,*.ogg,*.mp3,*.tres,*.ttf,*.otf,*.glb,*.gltf,*.obj"
            exclude_filter=""

            [preset.{next_idx}.options]

            custom_template/debug=""
            custom_template/release=""
            binary_format/embed_pck=false
            texture_format/s3tc_bptc=true
            texture_format/etc2_astc=false
        """)

        with open(presets_path, "a", encoding="utf-8") as f:
            f.write(win_cfg)
        return presets_path

    def export_project_windows(self, project_dir: str, output_dir: str | None = None) -> dict:
        """Export a Godot project as Windows Desktop (.exe + .pck).

        Args:
            project_dir: Path to the project root (containing project.godot)
            output_dir: Where to place exported files. Defaults to project_dir/_win_export/

        Returns:
            dict with 'ok', 'export_dir', 'exe_path', 'zip_path', 'files', 'error'
        """
        import zipfile

        if not self.is_available():
            return {"ok": False, "error": "Godot executable not found"}

        project_path = Path(project_dir).resolve()
        if not (project_path / "project.godot").exists():
            return {"ok": False, "error": "No project.godot found in project directory"}

        tpl_status = self.get_windows_template_status()
        if not tpl_status["installed"]:
            return {"ok": False, "error": tpl_status["hint"]}

        export_exe = self._find_stable_exe_for_export() or self._exe

        if output_dir:
            export_dir = Path(output_dir).resolve()
        else:
            export_dir = project_path / "_win_export"

        export_dir.mkdir(parents=True, exist_ok=True)

        # Derive exe name from project name
        project_name = project_path.name
        exe_path = export_dir / f"{project_name}.exe"

        self._write_windows_export_presets(project_dir)

        # Pre-export: force Godot to import all resources
        try:
            subprocess.run(
                [str(export_exe), "--path", str(project_path), "--headless", "--import"],
                capture_output=True,
                timeout=60,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as e:
            logger.warning("Resource import failed: %s, proceeding with export anyway", e)

        try:
            result = subprocess.run(
                [
                    str(export_exe), "--path", str(project_path),
                    "--headless", "--export-release", "Windows Desktop",
                    str(exe_path),
                ],
                capture_output=True,
                timeout=self.EXPORT_TIMEOUT,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )

            stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            combined = stdout + "\n" + stderr

            if not exe_path.exists():
                error_lines = [
                    line.strip() for line in combined.splitlines()
                    if line.strip() and any(kw in line.lower() for kw in
                                            ["error", "failed", "cannot", "invalid"])
                ]
                error_msg = "\n".join(error_lines[:10]) if error_lines else combined[-1000:]
                return {
                    "ok": False,
                    "error": f"Windows export failed (exit code {result.returncode}):\n{error_msg}",
                }

            # Package all exported files into a zip for download
            zip_path = export_dir / f"{project_name}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in sorted(export_dir.rglob("*")):
                    if f.is_file() and f != zip_path:
                        zf.write(f, f.relative_to(export_dir))

            exported_files = []
            for f in sorted(export_dir.rglob("*")):
                if f.is_file():
                    exported_files.append({
                        "name": f.name,
                        "path": str(f.relative_to(export_dir)),
                        "size": f.stat().st_size,
                    })

            return {
                "ok": True,
                "export_dir": str(export_dir),
                "exe_path": str(exe_path),
                "zip_path": str(zip_path),
                "zip_name": f"{project_name}.zip",
                "files": exported_files,
                "message": f"Windows export successful. {len(exported_files)} files generated.",
            }

        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"Export timed out after {self.EXPORT_TIMEOUT}s"}
        except Exception as e:
            return {"ok": False, "error": f"Windows export error: {e}"}
