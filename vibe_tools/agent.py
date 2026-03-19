"""LLM-powered game generation Agent - converts natural language to working Godot projects.

This is an Agent that uses tools to:
1. Generate a complete project plan (scenes + scripts)
2. Write files to disk
3. Validate the project using Godot's script checker
4. Read error output and fix broken scripts
5. Repeat until the project is valid

Uses SimpleLLMProvider from llm_transfer.py (OpenAI-compatible format with tool calling).
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from vibe_tools.llm_transfer import SimpleLLMProvider
from vibe_tools.models import (
    ASSET_EXTENSIONS_ALL, IGNORED_DIRS, PROJECT_SOURCE_EXTENSIONS,
    ASSET_EXTENSIONS_IMAGE, ASSET_EXTENSIONS_MODEL,
    classify_asset, is_ignored_path,
    ProjectPlan, ProjectSettings, GameType, SceneDesc, ScriptDesc,
    NodeDesc, ExportVar, OnReadyVar, FunctionDesc, InputEvent,
    ExtResource, SubResource,
)

logger = logging.getLogger(__name__)

# Vision / image-understanding model — used for ALL image analysis (sprite sheets, user screenshots, etc.)
# The main Agent (Claude Opus) NEVER receives raw images; this model describes them as text first.
VISION_MODEL = "gemini-2.5-flash"


# ─── Image Vision Analysis ───

def _analyze_image_with_vision(
    image_data: str,
    prompt: str,
    api_key: str | None = None,
    base_url: str | None = None,
) -> str:
    """Use Gemini Flash to analyze a single image with a custom prompt.

    The main Agent calls this via the `analyze_image` tool, controlling *what*
    to look for in the image (e.g. "describe the UI layout", "read the error
    message", "what sprites are in this sheet?").

    Args:
        image_data: base64 data URI string (e.g. "data:image/png;base64,...")
        prompt: the analysis instruction — what the Agent wants to know about the image
        api_key: LLM API key (falls back to env vars)
        base_url: LLM base URL (falls back to env vars)

    Returns:
        Text description / analysis result. On failure, a fallback error message.
    """
    effective_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY")
    effective_url = base_url or os.getenv("LLM_BASE_URL") or os.getenv("GEMINI_BASE_URL")

    if not effective_key:
        return "[Vision analysis unavailable — no API key configured]"

    llm = SimpleLLMProvider(model=VISION_MODEL, api_key=effective_key, base_url=effective_url)

    try:
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data}},
            ],
        }]
        response = llm.invoke(messages, temperature=0.2, max_tokens=2048)
        if response and response.strip():
            return response.strip()
        return "[Vision API returned empty response]"
    except Exception as e:
        logger.debug("Vision analysis failed: %s", e)
        return f"[Vision analysis failed: {e}]"


# ─── Sprite Sheet Analyzer ───

def _analyze_sprite_sheet(file_path: Path) -> dict[str, Any] | None:
    """Analyze an image to detect if it's a sprite sheet and extract grid metadata.

    Uses Pillow to read image dimensions, then analyzes transparency patterns
    to detect frame grid layout. Returns metadata dict or None if not a sprite sheet.
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(file_path) as img:
            width, height = img.size
            fmt = img.format or file_path.suffix.lstrip(".").upper()

            # Too small to be a sprite sheet (likely a single sprite)
            if width <= 64 and height <= 64:
                return {"width": width, "height": height, "is_sheet": False}

            # If image has no alpha channel, we can still try dimension-based heuristics
            has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info

            result: dict[str, Any] = {
                "width": width,
                "height": height,
                "format": fmt,
                "is_sheet": False,
                "detection_method": "none",
            }

            if has_alpha:
                # Convert to RGBA for analysis
                rgba = img.convert("RGBA") if img.mode != "RGBA" else img
                grid = _detect_grid_from_alpha(rgba, width, height)
                if grid:
                    result.update(grid)
                    result["is_sheet"] = True
                    result["detection_method"] = "alpha_scan"
                    return result

            # Fallback: dimension-based heuristic for common sprite sheet sizes
            grid = _guess_grid_from_dimensions(width, height)
            if grid:
                result.update(grid)
                result["is_sheet"] = True
                result["detection_method"] = "dimension_guess"

            return result
    except Exception as e:
        logger.debug("Failed to analyze sprite sheet %s: %s", file_path, e)
        return None


def _detect_grid_from_alpha(rgba_img: Any, width: int, height: int) -> dict[str, Any] | None:
    """Detect sprite sheet grid by scanning for transparent separator rows/columns.
    Uses Pillow batch operations for performance."""
    try:
        alpha_band = rgba_img.split()[3]
        alpha_bytes = alpha_band.tobytes()
    except Exception:
        return None

    # Build row and column alpha sums using fast byte iteration
    row_alpha_sum = [0] * height
    col_alpha_sum = [0] * width
    idx = 0
    for y in range(height):
        for x in range(width):
            a = alpha_bytes[idx]
            row_alpha_sum[y] += a
            col_alpha_sum[x] += a
            idx += 1

    # Adaptive threshold: a row/col is "empty" if its average alpha < 5 (out of 255)
    row_threshold = width * 5
    col_threshold = height * 5

    empty_rows = [i for i in range(height) if row_alpha_sum[i] <= row_threshold]
    empty_cols = [i for i in range(width) if col_alpha_sum[i] <= col_threshold]

    # Detect frame boundaries from empty rows/columns
    row_boundaries = _find_boundaries(empty_rows, height)
    col_boundaries = _find_boundaries(empty_cols, width)

    if len(row_boundaries) < 2 or len(col_boundaries) < 2:
        return None

    # Calculate frame dimensions
    frame_heights = [row_boundaries[i + 1] - row_boundaries[i] for i in range(len(row_boundaries) - 1)]
    frame_widths = [col_boundaries[i + 1] - col_boundaries[i] for i in range(len(col_boundaries) - 1)]

    if not frame_heights or not frame_widths:
        return None

    # Use most common frame size (mode)
    frame_h = max(set(frame_heights), key=frame_heights.count)
    frame_w = max(set(frame_widths), key=frame_widths.count)

    if frame_w < 8 or frame_h < 8:
        return None

    vframes = max(1, round(height / frame_h))
    hframes = max(1, round(width / frame_w))

    if hframes <= 1 and vframes <= 1:
        return None

    # Count non-empty frames per row
    rows_info = _detect_animation_rows(alpha_bytes, width, hframes, vframes, frame_w, frame_h)

    return {
        "hframes": hframes,
        "vframes": vframes,
        "frame_width": frame_w,
        "frame_height": frame_h,
        "total_frames": hframes * vframes,
        "rows_info": rows_info,
    }


def _find_boundaries(empty_indices: list[int], total_size: int) -> list[int]:
    """Find frame boundaries from a list of empty row/column indices."""
    if not empty_indices:
        return [0, total_size]

    boundaries = [0]
    prev = -2
    for idx in empty_indices:
        if idx - prev > 1:
            # Start of a new empty gap
            if prev >= 0:
                boundaries.append(prev + 1)  # End of previous gap is a boundary
        prev = idx

    # Add the last boundary
    if empty_indices[-1] < total_size - 1:
        boundaries.append(empty_indices[-1] + 1)
    boundaries.append(total_size)

    # Deduplicate and sort
    return sorted(set(boundaries))


def _detect_animation_rows(alpha_bytes: bytes, img_width: int,
                           hframes: int, vframes: int,
                           frame_w: int, frame_h: int) -> list[dict[str, Any]]:
    """Detect how many non-empty frames are in each row.
    Uses sampling (every 4th pixel) instead of scanning every pixel for performance.
    Does NOT guess animation names — names come from LLM vision analysis later."""
    rows_info = []
    total_pixels = len(alpha_bytes)
    sample_step = max(1, min(4, frame_w // 8))  # Sample every Nth pixel, at least every 4th

    for row in range(vframes):
        y_start = row * frame_h
        non_empty = 0
        for col in range(hframes):
            x_start = col * frame_w
            frame_has_content = False
            for y in range(y_start, min(y_start + frame_h, total_pixels // img_width), sample_step):
                row_offset = y * img_width
                for x in range(x_start, min(x_start + frame_w, img_width), sample_step):
                    pix_idx = row_offset + x
                    if pix_idx < total_pixels and alpha_bytes[pix_idx] > 10:
                        frame_has_content = True
                        break
                if frame_has_content:
                    break
            if frame_has_content:
                non_empty += 1

        if non_empty > 0:
            rows_info.append({
                "row": row,
                "frame_count": non_empty,
                "suggested_name": f"row_{row}",  # Placeholder — will be replaced by LLM vision
            })

    return rows_info


def _analyze_sprite_rows_with_vision(file_path: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Use Gemini Flash vision API to identify what animation each row of a sprite sheet represents.

    Sends the full sprite sheet image to the LLM with a structured prompt asking it to
    identify each row's animation. Updates meta['rows_info'] with real names.
    Falls back to generic names (row_0, row_1...) on failure.
    """
    rows_info = meta.get("rows_info", [])
    if not rows_info:
        return meta

    # Build the LLM API client using environment variables
    api_key = os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY")
    base_url = os.getenv("LLM_BASE_URL") or os.getenv("GEMINI_BASE_URL")
    if not api_key:
        logger.debug("No API key available for vision analysis — using generic row names")
        return meta

    try:
        from PIL import Image

        # Encode the full sprite sheet as base64 JPEG (smaller than PNG for API)
        with Image.open(file_path) as img:
            # If image is very large, resize for the API call
            max_dim = 1024
            if img.width > max_dim or img.height > max_dim:
                ratio = min(max_dim / img.width, max_dim / img.height)
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img = img.resize(new_size, Image.Resampling.NEAREST)

            # Convert to RGB for JPEG (drop alpha for smaller size)
            rgb_img = img.convert("RGB") if img.mode != "RGB" else img
            buf = io.BytesIO()
            rgb_img.save(buf, format="PNG")
            img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        vframes = meta.get("vframes", len(rows_info))
        hframes = meta.get("hframes", 1)
        frame_w = meta.get("frame_width", "?")
        frame_h = meta.get("frame_height", "?")

        row_desc_lines = []
        for ri in rows_info:
            row_desc_lines.append(f"Row {ri['row']}: {ri['frame_count']} frames")

        prompt = (
            f"This is a sprite sheet image for a 2D game character/object.\n"
            f"Grid: {hframes} columns × {vframes} rows, each frame is {frame_w}×{frame_h}px.\n"
            f"Rows with content:\n" + "\n".join(row_desc_lines) + "\n\n"
            f"For EACH row listed above, identify what animation/action it shows "
            f"(e.g., idle, walk, run, jump, fall, attack, hurt, die, climb, swim, shoot, dash, crouch, etc.).\n\n"
            f"Reply ONLY with a JSON array, one object per row, in this exact format:\n"
            f'[{{"row": 0, "name": "idle"}}, {{"row": 1, "name": "run"}}]\n'
            f"Use short lowercase snake_case names. If you can't tell, use \"unknown_N\"."
        )

        # Use the lightweight flash model for this quick vision task
        llm = SimpleLLMProvider(model=VISION_MODEL, api_key=api_key, base_url=base_url)

        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            ],
        }]

        response = llm.invoke(messages, temperature=0.1, max_tokens=512)
        if not response:
            logger.debug("Vision API returned empty response for sprite sheet analysis")
            return meta

        # Parse the JSON response
        # Strip markdown code fences if present
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```\w*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
            cleaned = cleaned.strip()

        vision_rows = json.loads(cleaned)
        if not isinstance(vision_rows, list):
            logger.debug("Vision API returned non-list: %s", type(vision_rows))
            return meta

        # Build a mapping: row number → name
        row_name_map: dict[int, str] = {}
        for item in vision_rows:
            if isinstance(item, dict) and "row" in item and "name" in item:
                row_name_map[int(item["row"])] = str(item["name"]).strip().lower().replace(" ", "_")

        # Update rows_info with vision-identified names
        for ri in rows_info:
            row_num = ri["row"]
            if row_num in row_name_map:
                ri["suggested_name"] = row_name_map[row_num]
                ri["name_source"] = "vision_ai"
            else:
                ri["name_source"] = "generic"

        meta["rows_info"] = rows_info
        meta["vision_analyzed"] = True
        logger.info("Vision API identified sprite row animations: %s",
                     {ri["row"]: ri["suggested_name"] for ri in rows_info})

    except json.JSONDecodeError as e:
        logger.debug("Failed to parse vision API response as JSON: %s", e)
    except ImportError:
        logger.debug("Pillow not available for vision analysis")
    except Exception as e:
        logger.debug("Vision analysis failed for sprite sheet %s: %s", file_path, e)

    return meta


def _guess_grid_from_dimensions(width: int, height: int) -> dict[str, Any] | None:
    """Fallback: guess grid layout from image dimensions using common frame sizes.
    Supports both square and non-square frames."""
    # Common frame sizes (width, height) — includes non-square
    common_square = [16, 24, 32, 48, 64, 96, 128, 256]
    common_rect = [(32, 48), (48, 64), (64, 96), (32, 64), (48, 32), (64, 48), (96, 64)]

    best: dict[str, Any] | None = None
    best_score = 0

    # Try square frames
    for fs in common_square:
        if width % fs == 0 and height % fs == 0:
            h = width // fs
            v = height // fs
            if h >= 2 or v >= 2:
                score = h * v
                if h >= 2 and v >= 2:
                    score *= 2
                if score > best_score:
                    best_score = score
                    best = {"hframes": h, "vframes": v, "frame_width": fs, "frame_height": fs,
                            "total_frames": h * v, "rows_info": []}

    # Try non-square frames
    for fw, fh in common_rect:
        if width % fw == 0 and height % fh == 0:
            h = width // fw
            v = height // fh
            if h >= 2 or v >= 2:
                score = h * v
                if h >= 2 and v >= 2:
                    score *= 2
                # Slight preference for frames with reasonable aspect ratio
                aspect = max(fw, fh) / min(fw, fh)
                if aspect <= 2.0:
                    score = int(score * 1.5)
                if score > best_score:
                    best_score = score
                    best = {"hframes": h, "vframes": v, "frame_width": fw, "frame_height": fh,
                            "total_frames": h * v, "rows_info": []}

    # Single-row sprite strip (width >> height)
    if best is None and width > height * 2:
        for fs in common_square:
            if height <= fs * 1.5 and width % fs == 0:
                h = width // fs
                if h >= 2:
                    best = {"hframes": h, "vframes": 1, "frame_width": fs, "frame_height": height,
                            "total_frames": h, "rows_info": []}
                    break

    # Single-column sprite strip (height >> width)
    if best is None and height > width * 2:
        for fs in common_square:
            if width <= fs * 1.5 and height % fs == 0:
                v = height // fs
                if v >= 2:
                    best = {"hframes": 1, "vframes": v, "frame_width": width, "frame_height": fs,
                            "total_frames": v, "rows_info": []}
                    break

    return best


def _format_sprite_sheet_info(meta: dict[str, Any], res_path: str) -> str:
    """Format sprite sheet metadata into a concise string for LLM injection."""
    w, h = meta["width"], meta["height"]

    if not meta.get("is_sheet"):
        return f"Single image ({w}×{h}px)"

    hf = meta["hframes"]
    vf = meta["vframes"]
    fw = meta["frame_width"]
    fh = meta["frame_height"]

    detection = meta.get("detection_method", "unknown")
    confidence = "high" if detection == "alpha_scan" else "low (dimension guess)"
    vision = meta.get("vision_analyzed", False)

    lines = [f"SPRITE SHEET ({w}×{h}px, grid {hf}×{vf}, frame {fw}×{fh}px, detection: {confidence})"]

    rows_info = meta.get("rows_info", [])
    if rows_info:
        for ri in rows_info[:8]:  # Max 8 rows shown
            source_tag = ""
            if ri.get("name_source") == "vision_ai":
                source_tag = " [AI-identified]"
            elif ri.get("name_source") == "generic":
                source_tag = " [unidentified]"
            lines.append(f"  Row {ri['row']} ({ri['frame_count']} frames): \"{ri['suggested_name']}\"{source_tag}")

    if not vision:
        lines.append(f"  ⚠️ Animation names are auto-detected and MAY BE WRONG — verify with the user if unsure")

    lines.append(f"  USE Sprite2D: hframes={hf}, vframes={vf}, frame=N")
    lines.append(f"  OR AnimatedSprite2D + SpriteFrames + AtlasTexture(region=Rect2(col*{fw}, row*{fh}, {fw}, {fh}))")
    lines.append(f"  ⚠️ DO NOT use this as a single texture — it will display the ENTIRE sheet!")

    return "\n".join(lines)


# ─── 3D Model Analyzer ───

def _analyze_3d_model(file_path: Path) -> dict[str, Any] | None:
    """Analyze a .glb/.gltf file to extract metadata: meshes, animations, materials, vertex count.

    Uses pygltflib to parse the file. Returns metadata dict or None on failure.
    """
    suffix = file_path.suffix.lower()
    if suffix not in (".glb", ".gltf"):
        return None

    try:
        from pygltflib import GLTF2
    except ImportError:
        logger.debug("pygltflib not installed — skipping 3D model analysis")
        return None

    try:
        gltf = GLTF2().load(str(file_path))

        result: dict[str, Any] = {
            "format": "GLB" if suffix == ".glb" else "glTF",
        }

        # Meshes
        mesh_names: list[str] = []
        total_primitives = 0
        if gltf.meshes:
            for mesh in gltf.meshes:
                mesh_names.append(mesh.name or f"Mesh_{len(mesh_names)}")
                if mesh.primitives:
                    total_primitives += len(mesh.primitives)
        result["meshes"] = mesh_names
        result["mesh_count"] = len(mesh_names)
        result["primitive_count"] = total_primitives

        # Approximate vertex count from accessors
        total_vertices = 0
        if gltf.meshes and gltf.accessors:
            for mesh in gltf.meshes:
                if mesh.primitives:
                    for prim in mesh.primitives:
                        if prim.attributes and hasattr(prim.attributes, "POSITION") and prim.attributes.POSITION is not None:
                            acc_idx = prim.attributes.POSITION
                            if 0 <= acc_idx < len(gltf.accessors):
                                total_vertices += gltf.accessors[acc_idx].count
        result["vertex_count"] = total_vertices

        # Animations
        anim_names: list[str] = []
        if gltf.animations:
            for anim in gltf.animations:
                anim_names.append(anim.name or f"Animation_{len(anim_names)}")
        result["animations"] = anim_names
        result["animation_count"] = len(anim_names)

        # Materials
        mat_names: list[str] = []
        if gltf.materials:
            for mat in gltf.materials:
                mat_names.append(mat.name or f"Material_{len(mat_names)}")
        result["materials"] = mat_names
        result["material_count"] = len(mat_names)

        # Nodes (scene hierarchy — useful to know root node type)
        node_count = len(gltf.nodes) if gltf.nodes else 0
        result["node_count"] = node_count

        # Skins (skeletal animation)
        has_skeleton = bool(gltf.skins) and len(gltf.skins) > 0
        result["has_skeleton"] = has_skeleton
        if has_skeleton:
            result["skin_count"] = len(gltf.skins)
            # Count joints
            total_joints = sum(len(s.joints) for s in gltf.skins if s.joints)
            result["joint_count"] = total_joints

        # Images/textures
        texture_count = len(gltf.textures) if gltf.textures else 0
        image_count = len(gltf.images) if gltf.images else 0
        result["texture_count"] = texture_count
        result["image_count"] = image_count

        return result

    except Exception as e:
        logger.debug("Failed to analyze 3D model %s: %s", file_path, e)
        return None


def _format_3d_model_info(meta: dict[str, Any], res_path: str) -> str:
    """Format 3D model metadata into a concise usage hint string."""
    parts = [meta.get("format", "3D")]

    vc = meta.get("vertex_count", 0)
    if vc:
        if vc > 1_000_000:
            parts.append(f"{vc / 1_000_000:.1f}M verts")
        elif vc > 1000:
            parts.append(f"{vc / 1000:.1f}K verts")
        else:
            parts.append(f"{vc} verts")

    mc = meta.get("mesh_count", 0)
    if mc:
        parts.append(f"{mc} mesh{'es' if mc > 1 else ''}")

    mat_c = meta.get("material_count", 0)
    if mat_c:
        parts.append(f"{mat_c} material{'s' if mat_c > 1 else ''}")

    lines = [f"3D MODEL ({', '.join(parts)})"]

    # Mesh names
    meshes = meta.get("meshes", [])
    if meshes and len(meshes) <= 10:
        lines.append(f"  Meshes: {', '.join(meshes)}")

    # Animations (CRITICAL info for LLM)
    anims = meta.get("animations", [])
    if anims:
        lines.append(f"  Animations ({len(anims)}): {', '.join(anims)}")
        lines.append(f"  → Play: $AnimationPlayer.play(\"{anims[0]}\")")
        lines.append(f"  ⚠️ Animation names are from the glTF file. Godot's importer may modify them")
        lines.append(f"     (e.g., strip 'Armature|' prefix). If playback fails, list animations at runtime:")
        lines.append(f"     for a in $AnimationPlayer.get_animation_list(): print(a)")
    else:
        lines.append("  No animations (static model)")

    # Skeleton
    if meta.get("has_skeleton"):
        joints = meta.get("joint_count", 0)
        lines.append(f"  Has skeleton ({joints} joints)")

    # Materials
    mats = meta.get("materials", [])
    if mats and len(mats) <= 8:
        lines.append(f"  Materials: {', '.join(mats)}")

    # Usage hints
    lines.append(f"  Instance in scene: use \"instance\": \"{res_path}\" in node JSON")
    lines.append(f"  GDScript: var m = preload(\"{res_path}\").instantiate(); add_child(m)")

    return "\n".join(lines)


# ─── Approximate token counting ───
# ~4 chars per token for English/code, conservative estimate

def _estimate_tokens(text: str) -> int:
    """Rough token count estimation (~4 chars per token)."""
    return len(text) // 4

def _estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens in a messages list."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += _estimate_tokens(part.get("text", ""))
                elif isinstance(part, dict) and part.get("type") == "image_url":
                    total += 300  # rough estimate for image tokens
    return total

# Maximum input context budget (leave room for output)
MAX_CONTEXT_TOKENS = 100_000  # most models support 128K+, leave 28K for output

# Memory budget: max tokens for project memory injection
MAX_MEMORY_TOKENS = 2_000  # ~8000 chars — prevents memory bloat from degrading LLM quality

# ─── Tool Definitions (OpenAI function calling format) ───

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "update_memory",
            "description": "Update the project memory file (memory.md) with important context about the current project. Call this to record: project architecture decisions, known issues, what was changed and why, game design details, etc. This memory persists across conversations so you can pick up where you left off.\n\n⚠️ MEMORY SIZE RULES (CRITICAL):\n- Keep memory UNDER 2000 characters total. If it's getting long, COMPRESS it.\n- DELETE resolved issues from ## Known Issues — don't keep fixed bugs.\n- ## Change Log: keep only the LAST 5 entries, remove older ones.\n- ## Project Overview and ## Architecture: keep concise (2-3 sentences each).\n- NEVER duplicate information across sections.\n- Think of memory as a BRIEF status snapshot, NOT a full history log.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Complete markdown content for the project memory file. MUST be under 2000 characters. Include: ## Project Overview (2-3 sentences), ## Architecture (key files only), ## Known Issues (ONLY unresolved), ## Change Log (last 5 entries only). Remove resolved issues and old changelog entries to stay concise."
                    }
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_project",
            "description": "Generate a complete Godot project. Provide the project specification as structured parameters. raw_code in scripts must use actual newlines and tabs (the JSON will handle escaping). This creates all files on disk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Project name"
                    },
                    "game_type": {
                        "type": "string",
                        "enum": ["blank", "platformer_2d", "topdown_2d", "fps_3d", "tps_3d"],
                        "description": "Game type category"
                    },
                    "settings": {
                        "type": "object",
                        "description": "Project settings",
                        "properties": {
                            "window_width": {"type": "integer"},
                            "window_height": {"type": "integer"},
                            "stretch_mode": {"type": "string"},
                            "stretch_aspect": {"type": "string"}
                        }
                    },
                    "scenes": {
                        "type": "array",
                        "description": "Scene files to generate. Each has filename and root node tree.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "filename": {"type": "string"},
                                "root": {"type": "object", "description": "Root node with name, type, properties, script, instance, groups, children. Use 'instance' (res:// path to .glb/.tscn) to instance a PackedScene instead of creating a typed node. When using instance, 'type' is ignored."}
                            }
                        }
                    },
                    "scripts": {
                        "type": "array",
                        "description": "Script files to generate. Each has filename, extends, and raw_code.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "filename": {"type": "string"},
                                "extends": {"type": "string"},
                                "raw_code": {"type": "string", "description": "Complete GDScript code. Use \\n for newlines and \\t for tabs."}
                            }
                        }
                    }
                },
                "required": ["name", "game_type", "scenes", "scripts"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "validate_project",
            "description": "Validate the generated project. Checks for structural issues (missing files, bad references, invalid node types) and GDScript syntax errors. Always call this after generating or modifying a project.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the generated project. Use this to inspect a script or scene file before fixing it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Filename relative to project root, e.g. 'main.gd', 'player.tscn'"
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write/overwrite a file in the generated project. Use this to fix scripts or scene files that have errors. For .gd files use TAB indentation. For .tscn files use Godot 4.x format=3 syntax.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Filename relative to project root, e.g. 'main.gd', 'player.tscn'"
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete file content to write"
                    }
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all files in the generated project directory.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_assets",
            "description": "List all available asset files (images, audio, 3D models, fonts) uploaded by the user to the project. Returns the res:// path you can use in scripts and scenes. ALWAYS call this before generating a project to check if the user has uploaded any assets you should use.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the project. Use this to remove obsolete scripts, scenes, or resources.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Filename relative to project root, e.g. 'old_player.gd'"
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rename_file",
            "description": "Rename/move a file within the project. Also updates references in .tscn and .gd files automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_filename": {
                        "type": "string",
                        "description": "Current filename relative to project root"
                    },
                    "new_filename": {
                        "type": "string",
                        "description": "New filename relative to project root"
                    }
                },
                "required": ["old_filename", "new_filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for text/pattern across all project files. Returns matching lines with file names and line numbers. Useful for finding where a function, variable, signal, or node is used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or regex pattern to search for"
                    },
                    "file_extensions": {
                        "type": "string",
                        "description": "Comma-separated extensions to filter, e.g. '.gd,.tscn'. Empty = all files."
                    }
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_project_settings",
            "description": "Modify project.godot settings. Use this to change input mappings, window size, physics layers, render settings, autoloads, etc. Provide key-value pairs in Godot config format.",
            "parameters": {
                "type": "object",
                "properties": {
                    "settings": {
                        "type": "object",
                        "description": "Dictionary of section/key/value. Example: {\"input/move_left\":{\"deadzone\":0.5,\"events\":[...]}, \"display/window/size/viewport_width\":1280}. Use Godot project.godot format."
                    },
                    "raw_append": {
                        "type": "string",
                        "description": "Raw text to append to project.godot (for complex settings like input maps). Will be appended at the end of the file."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_project",
            "description": "Run the Godot project and capture output/errors. Returns stdout/stderr from a short run (auto-closes after timeout). Use this to test if the game starts without runtime errors.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "How many seconds to let the game run before auto-closing. Default: 5"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_resource",
            "description": "Create a Godot .tres resource file. Use this for TileSet, Theme, Material, Gradient, StyleBox, AudioBusLayout, Animation, or any other resource type. Provide the complete .tres file content in Godot 4.x format.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Resource filename relative to project root, e.g. 'default_theme.tres', 'materials/floor.tres'"
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete .tres file content in Godot 4.x resource format"
                    }
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "patch_file",
            "description": "Make targeted edits to a file without rewriting the entire content. Provide one or more search-replace pairs. More efficient than write_file for small changes to large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Filename relative to project root"
                    },
                    "patches": {
                        "type": "array",
                        "description": "Array of {old: string, new: string} pairs. Each 'old' must be an exact substring found in the file.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old": {"type": "string", "description": "Exact text to find"},
                                "new": {"type": "string", "description": "Replacement text"}
                            },
                            "required": ["old", "new"]
                        }
                    }
                },
                "required": ["filename", "patches"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "export_web",
            "description": "Export the current Godot project as an HTML5/Web build so users can play it directly in a browser without installing Godot. The exported game will be available at a shareable URL. Call this after the project is validated and working.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Analyze a user-attached image using vision AI (Gemini Flash). You CANNOT see images directly — use this tool to understand what an image shows. Provide a specific prompt describing what you want to know about the image (e.g. 'describe this UI screenshot', 'read the error message in this image', 'what sprites/objects are in this sprite sheet?', 'describe the game scene layout'). The user's attached images are numbered starting from 1.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_index": {
                        "type": "integer",
                        "description": "Which image to analyze (1-based index). The user's message will tell you how many images are attached."
                    },
                    "prompt": {
                        "type": "string",
                        "description": "What to analyze/look for in the image. Be specific — e.g. 'Describe the game UI layout and all visible elements', 'Read any error messages or code visible in this screenshot', 'List all sprites/characters visible and their positions'."
                    }
                },
                "required": ["image_index", "prompt"]
            }
        }
    },
]

# ─── Tool-Prompt Bindings ───
# Each tool can have static prompts (always injected) and dynamic prompts
# (injected based on tool args, e.g. file extension).
# Prompts are injected into tool results automatically on first use per session.

TOOL_PROMPTS: dict[str, list[str]] = {
    # Project generation needs core Godot knowledge + scene format
    "generate_project": ["godot4_basics", "tscn_format", "signals_patterns"],
    # Resource creation needs scene format knowledge
    "create_resource": ["tscn_format"],
    # Input/settings configuration
    "edit_project_settings": ["input_handling"],
}

# Dynamic prompt rules: based on tool args content / file extension
# Format: (tool_name, arg_condition_fn) → [prompts]
# These are checked at runtime for context-sensitive injection.
def _get_dynamic_prompts(tool_name: str, args: dict, tool_result: str) -> list[str]:
    """Determine additional prompts to inject based on tool args and result context."""
    prompts: list[str] = []

    filename = args.get("filename", "")

    if tool_name in ("write_file", "patch_file", "read_file"):
        if filename.endswith(".tscn"):
            prompts.append("tscn_format")
        elif filename.endswith(".gd"):
            prompts.append("godot4_basics")
        # If the file references 3D models, inject 3D guide
        if ".glb" in filename or ".gltf" in filename:
            prompts.append("3d_models")

    # If list_assets found sprite sheets, inject sprite sheet guide
    if tool_name == "list_assets" and "is_sprite_sheet" in tool_result:
        prompts.append("sprite_sheet")

    # If list_assets found 3D models, inject 3D model guide
    if tool_name == "list_assets" and '"category": "model"' in tool_result:
        prompts.append("3d_models")

    # If generate_project references .glb/.gltf or uses instance field, inject 3D guide
    if tool_name == "generate_project":
        args_str = str(args)
        if ".glb" in args_str or ".gltf" in args_str or '"instance"' in args_str or "'instance'" in args_str:
            prompts.append("3d_models")

    # If writing a scene or script that references 3D models
    if tool_name in ("write_file", "patch_file"):
        content = args.get("content", "")
        patches_str = str(args.get("patches", ""))
        combined = content + patches_str
        if ".glb" in combined or ".gltf" in combined or "AnimationPlayer" in combined:
            prompts.append("3d_models")

    # If read_file result contains 3D model references, inject 3D guide for context
    if tool_name == "read_file":
        if ".glb" in tool_result or ".gltf" in tool_result or "AnimationPlayer" in tool_result:
            prompts.append("3d_models")

    return prompts


def _load_prompt_file(name: str) -> str | None:
    """Load a prompt .md file by name. Returns content or None."""
    prompt_path = Path(__file__).parent / "prompts" / f"{name}.md"
    if prompt_path.exists():
        try:
            return prompt_path.read_text(encoding="utf-8")
        except Exception:
            return None
    return None

# ─── System Prompt ───

SYSTEM_PROMPT = """You are GodotVibe, an AI game development assistant for Godot 4.x.
Respond in the SAME LANGUAGE as the user.

## HOW YOU WORK:
- **Chatting** (greetings, questions): Just reply with text, no tool calls
- **Building/modifying**: Briefly acknowledge, call tools, then summarize what you changed and why
- Tool-calling IS your intent detection — no separate classification needed

## COMMUNICATION RULES (CRITICAL):
- **ALWAYS talk to the user.** After making changes, you MUST explain what you did and why in natural language.
- **Bug fix requests**: Address EACH bug the user mentioned. Explain what caused each bug and how you fixed it. Do NOT just say "all fixed" or "running perfectly" — be SPECIFIC.
- **NEVER end with only tool calls.** Your LAST message in a conversation MUST contain a natural-language reply to the user explaining what was done.
- If the user reported multiple issues, respond with a numbered list addressing each one.
- If you attached images/screenshots showing bugs, acknowledge what you SEE in the images.

## CONTEXT PRIORITY (CRITICAL):
- **The user's CURRENT message is your #1 priority.** Always read it carefully and respond to exactly what it says.
- Project memory and chat history are background reference — they may be OUTDATED. The user's current request may describe NEW issues not in memory.
- **Do NOT confuse old issues with new ones.** If the user reports a new bug, don't respond about a previously fixed bug.
- When in doubt, READ the actual project files to understand the current state — don't rely on memory alone.

## IMAGES & SCREENSHOTS:
When the user sends images/screenshots:
- **CAREFULLY EXAMINE** every image. Describe what you see before acting.
- If it's a screenshot of a bug/error: identify the EXACT issue shown, then fix it.
- If it's a reference image (design mockup, sprite, game screenshot): replicate the visual style, layout, and elements as closely as possible.
- If it's an asset/sprite: use it directly via `list_assets` + preload().
- **NEVER ignore images.** They are the user's primary way of showing you what's wrong or what they want.

## WORKFLOW:

### TASK PLANNING (CRITICAL — prevents incomplete work):
For any request that requires creating/modifying **2 or more files**, you MUST:
1. **First, state your plan** in your text reply: list ALL files you will create/modify and what each does.
2. **Then execute ALL steps** — create every file, wire every signal, connect every scene.
3. **Do NOT stop after creating just one or two files.** Keep calling tools until EVERY item in your plan is done.
4. **After ALL files are created/modified**, call `validate_project`, fix any errors, then `update_memory`.
5. **Your final reply must summarize EVERYTHING you did**, not just the last file.

Example plan for "make a game with gems and a snake":
> Plan: I will create (1) gem.tscn + gem.gd, (2) snake.tscn + snake.gd, (3) hud.tscn + hud.gd for score, (4) update main.tscn to include all scenes, (5) update player.gd for collection logic.
Then execute ALL 5 steps before stopping.

### NEW project:
1. `list_assets` → `generate_project` (ONCE only) → `validate_project` → fix errors → `update_memory`
2. Use ALL uploaded assets. Do NOT use ColorRect when real images exist.

### EXISTING project:
- `list_files`/`read_file` to understand → `patch_file`/`write_file` to fix → `validate_project` → `update_memory`
- Do NOT call `generate_project` — it already exists!
- **FOCUS on the user's specific request.** Fix what they asked about, nothing else.
- **NEVER ask the user for debug output, console logs, or to run the game and report back.** You have tools to diagnose problems yourself. Diagnose and fix problems AUTONOMOUSLY.
- **Self-debug workflow**: `read_file` (read the broken script) → `run_project` (capture errors, if available) → analyze → `patch_file`/`write_file` (fix) → `validate_project` → done. Do NOT ask the user to do any of these steps for you.
- **If `run_project` is unavailable** (Godot executable not found): Do NOT fall back to asking the user for runtime output. Instead, reason from the code itself — read the script, identify bugs by code analysis, and fix them. For 3D models/animations, ALWAYS use the runtime discovery pattern (see 3D MODEL GUIDE) — never hardcode animation names regardless of whether you can run the project.

### PRESERVATION RULE (CRITICAL — prevents regression):
When fixing bugs in an existing project, you MUST:
- **Read the file FIRST** with `read_file` before making ANY changes.
- **Use `patch_file` for targeted fixes** — change ONLY the broken parts.
- **NEVER rewrite an entire file** unless the user explicitly asks for a full rewrite. Overwriting a working file with a new version often deletes existing logic (animation discovery, collision setup, camera controls, etc.), causing NEW bugs.
- **NEVER remove existing functionality** that wasn't part of the bug report. If the user says "animation doesn't play", fix the animation code — don't delete the movement, camera, or physics code.
- **NEVER replace runtime discovery patterns with hardcoded values.** If the existing code discovers animations at runtime via `get_animation_list()`, keep that pattern. Do NOT replace it with hardcoded animation names like "A_TPose", "Idle_Loop", etc.
- If `patch_file` fails (old text doesn't match), re-read the file with `read_file` and try again with the correct text. Do NOT fall back to `write_file` with a full rewrite.

## KEY RULES:
1. `generate_project` can only be called ONCE. After that, use read+write/patch.
2. Always `read_file` before `write_file`/`patch_file`.
3. **Prefer `patch_file` for small changes. NEVER use `write_file` to fully rewrite a working file — this causes regressions.**
4. Always `validate_project` after changes.
5. Always `update_memory` when done.
6. **Answer the user's actual question.** Don't guess — read the relevant files first.
7. **Be efficient with iterations.** If you changed files, keep fixing until validate passes with 0 errors. But if the task is simple (just answering a question, small tweak already done), respond and stop — don't waste iterations.
8. **NEVER ask the user to provide debug info, console output, animation names, or any runtime data.** You are a fully autonomous agent — solve problems yourself by reading code and reasoning about it.
9. **NEVER add debug print statements and ask the user to report what they print.** If you need runtime info and `run_project` works, use it directly. If `run_project` is unavailable, reason from code alone.

## BUILT-IN GUIDES:
- When you call tools, relevant Godot guides are automatically included in the result under `__guide__`. READ THEM — they contain critical syntax, patterns, and gotchas.
- These guides are injected only once per topic per session, so pay attention when they appear.

## GODOT 4.x (NOT 3.x!):
- GDScript uses TAB indentation
- `CharacterBody2D` (not KinematicBody2D), `Node3D` (not Spatial), `@export` (not export), `@onready` (not onready)
- `move_and_slide()` takes NO args — `velocity` is a property
- `await` not `yield`, `instantiate()` not `instance()`, `TileMapLayer` not `TileMap`
- CollisionShape2D/3D MUST have a SubResource shape
- .tscn format=3, root node has NO parent, children have parent="."

## ASSET LOADING (CRITICAL — EXPORT BREAKS OTHERWISE):
- **ALWAYS** load assets via `load("res://...")` or `preload("res://...")`. These work in both editor AND exported builds.
- **NEVER** use `Image.load()` with `ProjectSettings.globalize_path()`. This reads from the absolute disk path, which does NOT exist in exported builds (assets are packed inside .pck/.exe). The game will show blank squares instead of images.
- **NEVER** create an "AssetLoader" that "bypasses the import system" with `Image.new()` + `img.load(abs_path)`. This is a guaranteed export-breaking pattern.
- For sprite sheets: use `load("res://assets/sprite.png")` to get the texture, then set `hframes`/`vframes` on a `Sprite2D`.
- For audio: use `load("res://assets/sound.ogg")` and assign to `AudioStreamPlayer.stream`.
- Correct pattern: `var tex: Texture2D = load("res://assets/Foxy.png")` ← works everywhere
- Wrong pattern: `var img = Image.new(); img.load(ProjectSettings.globalize_path("res://assets/Foxy.png"))` ← BREAKS on export
"""


# ─── Helper Functions ───

def _parse_node(data: dict[str, Any]) -> NodeDesc:
    """Recursively parse a node description from JSON."""
    children = [_parse_node(c) for c in data.get("children", [])]
    return NodeDesc(
        name=data.get("name", "Node"),
        type=data.get("type", "Node"),
        properties=data.get("properties", {}),
        script=data.get("script"),
        instance=data.get("instance"),
        groups=data.get("groups", []),
        children=children,
    )


def _parse_plan_json(raw: dict[str, Any], output_dir: str) -> ProjectPlan:
    """Convert parsed JSON into a ProjectPlan."""
    settings_data = raw.get("settings", {})
    settings = ProjectSettings(
        name=raw.get("name", "VibeGame"),
        main_scene="res://" + (raw.get("scenes", [{}])[0].get("filename", "main.tscn") if raw.get("scenes") else "main.tscn"),
        window_width=settings_data.get("window_width", 1152),
        window_height=settings_data.get("window_height", 648),
        stretch_mode=settings_data.get("stretch_mode", "canvas_items"),
        stretch_aspect=settings_data.get("stretch_aspect", "expand"),
    )

    input_actions = settings_data.get("input_actions", {})
    for action_name, events in input_actions.items():
        parsed_events = []
        for evt in events:
            parsed_events.append(InputEvent(
                type=evt.get("type", "key"),
                value=evt.get("value", ""),
            ))
        settings.input_actions[action_name] = parsed_events

    game_type_str = raw.get("game_type", "blank")
    try:
        game_type = GameType(game_type_str)
    except ValueError:
        game_type = GameType.BLANK

    scenes: list[SceneDesc] = []
    for scene_data in raw.get("scenes", []):
        root = _parse_node(scene_data.get("root", {"name": "Root", "type": "Node"}))
        scenes.append(SceneDesc(
            filename=scene_data.get("filename", "main.tscn"),
            root=root,
        ))

    scripts: list[ScriptDesc] = []
    for script_data in raw.get("scripts", []):
        scripts.append(ScriptDesc(
            filename=script_data.get("filename", "script.gd"),
            extends=script_data.get("extends", "Node"),
            raw_code=script_data.get("raw_code"),
        ))

    return ProjectPlan(
        name=raw.get("name", "VibeGame"),
        game_type=game_type,
        output_dir=output_dir,
        settings=settings,
        scenes=scenes,
        scripts=scripts,
    )


def _validate_instance_paths(node: NodeDesc, project_dir: str, warnings: list[str], node_path: str = "") -> None:
    """Recursively validate instance paths in a node tree.
    Checks that instance files exist, and warns about instance+children conflicts."""
    current_path = f"{node_path}/{node.name}" if node_path else node.name

    if node.instance:
        # Validate that the instance file exists
        res_path = node.instance
        if res_path.startswith("res://"):
            rel_path = res_path[6:]  # Strip "res://"
            full_path = Path(project_dir) / rel_path
            if not full_path.exists():
                warnings.append(
                    f"⚠️ Node '{current_path}': instance path '{res_path}' — file not found at '{full_path}'. "
                    f"Check the path is correct (e.g., res://assets/model.glb, not res://asset/model.glb)."
                )

        # Warn about instance + children conflict
        if node.children:
            child_types = [c.type for c in node.children]
            anim_conflict = [t for t in child_types if t in ("AnimationPlayer", "AnimationTree")]
            if anim_conflict:
                warnings.append(
                    f"🚫 CRITICAL: Node '{current_path}' is an instanced .glb/.tscn and has "
                    f"{', '.join(anim_conflict)} as children. GLB files ALREADY contain their own "
                    f"AnimationPlayer — adding duplicates causes conflicts. "
                    f"Remove these children and access animations via GDScript: "
                    f"model.find_child(\"AnimationPlayer\")"
                )
            warnings.append(
                f"⚠️ Node '{current_path}': has both 'instance' and 'children'. "
                f"Children will be IGNORED for instanced nodes in .tscn. "
                f"Move children to the PARENT wrapper node instead. "
                f"Correct: CharacterBody3D → [Model(instance=glb), CollisionShape3D, CameraPivot] "
                f"Wrong: CharacterBody3D → Model(instance=glb) → [CollisionShape3D, CameraPivot]"
            )

    for child in node.children:
        _validate_instance_paths(child, project_dir, warnings, current_path)


def _extract_json(text: str) -> dict[str, Any]:
    """Extract JSON from LLM response (handles markdown code blocks)."""
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    patterns = [
        r"```json\s*\n(.*?)\n```",
        r"```\s*\n(.*?)\n```",
        r"\{[\s\S]*\}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            candidate = match.group(1) if match.lastindex else match.group(0)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

    raise ValueError(f"Could not extract valid JSON from LLM response:\n{text[:500]}")


# ─── Agent Step Tracking ───

class AgentStep:
    """Represents a single step in the agent's execution."""

    def __init__(self, step_type: str, description: str, details: str = ""):
        self.step_type = step_type  # "thinking", "tool_call", "tool_result", "error", "success"
        self.description = description
        self.details = details

    def to_dict(self) -> dict:
        return {
            "type": self.step_type,
            "description": self.description,
            "details": self.details,
        }



# ─── Chat History Store (shared, thread-safe) ───

class ChatHistoryStore:
    """Thread-safe per-project chat history storage.

    Shared across all AgentSessions but protected by a lock so concurrent
    requests operating on different projects don't corrupt each other.
    """

    def __init__(self):
        self._histories: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, project_dir: str | None = None) -> list[dict[str, Any]]:
        """Get a COPY of chat history for a project (safe for concurrent reads)."""
        key = str(Path(project_dir).resolve()) if project_dir else "__global__"
        with self._lock:
            if key not in self._histories:
                self._histories[key] = []
            return list(self._histories[key])  # Return copy

    def append(self, project_dir: str | None, msg: dict[str, Any]) -> None:
        """Append a message to project-specific chat history (thread-safe)."""
        key = str(Path(project_dir).resolve()) if project_dir else "__global__"
        with self._lock:
            if key not in self._histories:
                self._histories[key] = []
            self._histories[key].append(msg)
            # Keep bounded (last 10 — recent context is enough, too much old history confuses the LLM)
            if len(self._histories[key]) > 10:
                del self._histories[key][:len(self._histories[key]) - 10]

    def clear(self, project_dir: str | None = None) -> None:
        """Clear chat history for a specific project."""
        if project_dir:
            key = str(Path(project_dir).resolve())
        else:
            key = "__global__"
        with self._lock:
            self._histories.pop(key, None)

    def clear_all(self) -> None:
        """Clear all chat histories."""
        with self._lock:
            self._histories.clear()


# ─── Agent Session (per-request isolated state) ───

class AgentSession:
    """Per-request isolated state for an agent execution.

    Each API request creates its own AgentSession, ensuring that concurrent
    requests from different users/tabs don't share mutable state like
    _output_dir, _steps, _step_callback, _project_generated, etc.
    """

    # Dynamic iteration limits based on task type
    MAX_ITERATIONS_NEW = 25       # New project: generate + validate + fix + assets + memory
    MAX_ITERATIONS_EXISTING = 20  # Existing project: read + fix + validate + memory
    MAX_ITERATIONS_CHAT = 3       # Pure chat: just respond

    def __init__(self, llm: SimpleLLMProvider, chat_store: ChatHistoryStore, runner: Any = None):
        self.llm = llm
        self._chat_store = chat_store
        self._runner = runner
        self._output_dir: str | None = None
        self._steps: list[AgentStep] = []
        self._step_callback: Any = None
        self._project_generated = False  # Lock: only allow generate_project once per session
        self._last_plan: ProjectPlan | None = None
        self._files_read_this_loop: set[str] = set()  # Track which files were read
        # Validation tracking
        self._post_validation_count: int = 0
        self._has_validated: bool = False
        self._has_file_changes: bool = False
        # Track which prompt guides have been injected (avoid duplicates)
        self._injected_prompts: set[str] = set()
        # User-attached images for this session (base64 data URIs), accessible via analyze_image tool
        self._images: list[str] = []

    def _get_runner(self):
        """Lazy-init GodotRunner."""
        if self._runner is None:
            from vibe_tools.godot_runner import GodotRunner
            try:
                self._runner = GodotRunner()
            except FileNotFoundError:
                self._runner = None
        return self._runner

    # ─── Read-tracking for the current agent loop ───

    def _mark_file_read(self, filename: str) -> None:
        """Record that a file was read in this agent loop iteration."""
        self._files_read_this_loop.add(filename)

    def _was_file_read(self, filename: str) -> bool:
        """Check if a file was read during this agent loop."""
        return filename in self._files_read_this_loop

    def _add_step(self, step_type: str, description: str, details: str = ""):
        """Record an agent step and notify callback if set."""
        step = AgentStep(step_type, description, details)
        self._steps.append(step)
        if self._step_callback:
            self._step_callback(step)
        logger.info(f"Agent step [{step_type}]: {description}")

    # ─── Path Safety ───

    def _safe_project_path(self, filename: str) -> Path | None:
        """Resolve *filename* inside the project dir, rejecting path traversal.

        Returns the resolved Path, or None if the path escapes the project.
        """
        if not self._output_dir:
            return None
        project_root = Path(self._output_dir).resolve()
        target = (project_root / filename).resolve()
        try:
            target.relative_to(project_root)
        except ValueError:
            logger.warning("Path traversal attempt blocked: %s", filename)
            return None
        return target

    # ─── Tool Implementations ───

    def _tool_generate_project(self, args: dict) -> str:
        """Tool: generate_project - create project files from structured args. Only callable ONCE."""
        if self._project_generated:
            return json.dumps({
                "ok": False,
                "error": "Project already generated! You MUST NOT call generate_project again. "
                         "Use read_file to inspect files with errors, then write_file to fix them, "
                         "then validate_project to check again."
            })

        try:
            raw = args
            plan = _parse_plan_json(raw, self._output_dir)

            # Validate instance paths before generating
            instance_warnings: list[str] = []
            for scene in plan.scenes:
                _validate_instance_paths(scene.root, self._output_dir, instance_warnings)

            from vibe_tools.project_generator import ProjectGenerator
            gen = ProjectGenerator()
            out = gen.generate(plan)

            self._last_plan = plan
            self._project_generated = True  # Lock it

            out_path = Path(out)
            files = []
            for f in out_path.rglob("*"):
                if f.is_file() and f.suffix in PROJECT_SOURCE_EXTENSIONS:
                    files.append(str(f.relative_to(out_path)).replace("\\", "/"))

            # Verify that uploaded assets are actually used in generated scripts
            missing_assets = self._verify_assets_in_scripts()
            result_data = {
                "ok": True,
                "files": files,
                "message": f"Project '{plan.name}' generated with {len(plan.scenes)} scene(s) and {len(plan.scripts)} script(s). "
                           f"Now call validate_project to check for errors."
            }

            if missing_assets:
                result_data["ASSET_WARNING"] = (
                    f"⚠️ CRITICAL: {len(missing_assets)} uploaded asset(s) are NOT referenced in any script or scene! "
                    f"The user uploaded these files specifically to be used in the game. "
                    f"You MUST fix this IMMEDIATELY by reading the relevant scripts and adding preload() references.\n"
                    f"Missing assets:\n" + "\n".join(f"  - {a}" for a in missing_assets) + "\n"
                    f"After validate_project, use read_file + patch_file/write_file to add these asset references to appropriate scripts."
                )

            if instance_warnings:
                result_data["INSTANCE_WARNINGS"] = (
                    f"⚠️ {len(instance_warnings)} instance path issue(s):\n"
                    + "\n".join(f"  - {w}" for w in instance_warnings)
                )

            return json.dumps(result_data)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def _tool_validate_project(self, args: dict) -> str:
        """Tool: validate_project - check project for errors. Returns file contents for broken files."""
        if not self._output_dir:
            return json.dumps({"ok": False, "error": "No project generated yet"})

        project_path = Path(self._output_dir)
        if not project_path.exists():
            return json.dumps({"ok": False, "error": "Project directory not found"})

        runner = self._get_runner()
        errors = []

        # 1. Structural validation (always run, doesn't need Godot exe)
        struct_errors = self._structural_validate(self._output_dir)
        errors.extend(struct_errors)

        # 1.5 Check for export-breaking anti-patterns in GDScript files
        for gd_file in project_path.rglob("*.gd"):
            rel = str(gd_file.relative_to(project_path)).replace("\\", "/")
            try:
                content = gd_file.read_text(encoding="utf-8")
                if "globalize_path" in content and "img.load" in content.lower():
                    errors.append(
                        f"{rel}: ⚠️ EXPORT-BREAKING PATTERN: Uses Image.load() with globalize_path(). "
                        f"This reads from the absolute disk path which does NOT exist in exported builds! "
                        f"Replace with: var tex = load(\"res://...\") to load textures. "
                        f"NEVER use Image.new() + img.load(ProjectSettings.globalize_path(...))."
                    )
            except Exception:
                pass

        # 2. Check each GDScript file for syntax/semantic errors
        script_results = {}
        if runner and runner.is_available():
            for gd_file in project_path.rglob("*.gd"):
                rel = str(gd_file.relative_to(project_path)).replace("\\", "/")
                result = runner.check_gdscript(self._output_dir, f"res://{rel}")
                script_results[rel] = result
                if not result["ok"]:
                    for err in result["errors"]:
                        errors.append(f"{rel}: {err}")
        else:
            # If no Godot exe, do basic Python-level validation
            for gd_file in project_path.rglob("*.gd"):
                rel = str(gd_file.relative_to(project_path)).replace("\\", "/")
                try:
                    content = gd_file.read_text(encoding="utf-8")
                    basic_errors = self._basic_gdscript_check(content, rel)
                    if basic_errors:
                        errors.extend(basic_errors)
                        script_results[rel] = {"ok": False, "errors": basic_errors}
                    else:
                        script_results[rel] = {"ok": True, "errors": []}
                except Exception as e:
                    errors.append(f"{rel}: Cannot read file: {e}")

        if errors:
            # Collect the CONTENT of files that have errors so LLM can fix them directly
            error_files: set[str] = set()
            for err in errors:
                # Extract filename from error like "main.gd: some error" or "main.tscn: ..."
                if ": " in err:
                    fname = err.split(":")[0].strip()
                    if fname.endswith((".gd", ".tscn", ".tres", ".godot")):
                        error_files.add(fname)

            file_contents: dict[str, str] = {}
            for fname in error_files:
                fpath = project_path / fname
                if fpath.exists():
                    try:
                        content = fpath.read_text(encoding="utf-8")
                        # Add line numbers for easy reference
                        numbered = "\n".join(
                            f"{i+1:4d}: {line}"
                            for i, line in enumerate(content.split("\n"))
                        )
                        file_contents[fname] = numbered
                    except Exception:
                        pass

            result_data = {
                "ok": False,
                "errors": errors,
                "error_count": len(errors),
                "message": f"Found {len(errors)} error(s). "
                           f"DO NOT call generate_project again! "
                           f"Read the errors below and the file contents, then use write_file to fix each file.",
            }

            # Include file contents directly so LLM can fix without extra read_file calls
            if file_contents:
                result_data["file_contents"] = file_contents
                result_data["fix_instructions"] = (
                    "The files with errors are shown above with line numbers. "
                    "For each file with errors: analyze the error, find the problematic line, "
                    "fix it, and write the COMPLETE corrected file using write_file. "
                    "Then call validate_project again."
                )

            return json.dumps(result_data)
        else:
            # Validation passed — now check assets usage
            missing_assets = self._verify_assets_in_scripts()
            result = {
                "ok": True,
                "message": "All validations passed! Project is ready to run.",
                "script_results": script_results
            }
            if missing_assets:
                result["ok"] = False
                result["message"] = (
                    f"Syntax validation passed, but {len(missing_assets)} uploaded asset(s) are NOT USED in the project! "
                    f"The user uploaded these assets for a reason — you MUST use them all."
                )
                result["unused_assets"] = missing_assets
                result["fix_instructions"] = (
                    "For each unused asset:\n"
                    "1. read_file the most relevant script (e.g. player.gd for player images, main.gd for BGM)\n"
                    "2. Use patch_file to add preload() and usage code\n"
                    "3. Call validate_project again after fixing\n\n"
                    "Asset mapping guide:\n"
                    + "\n".join(f"  - {a}" for a in missing_assets)
                )
            return json.dumps(result)

    def _basic_gdscript_check(self, content: str, filename: str) -> list[str]:
        """Basic Python-level GDScript checks (used when Godot exe not available)."""
        errors = []
        lines = content.split("\n")

        if not lines or not lines[0].startswith("extends"):
            if not any(l.startswith("extends") for l in lines[:5]):
                errors.append(f"{filename}: Missing 'extends' declaration at top of file")

        # Check for space indentation (should be tabs)
        for i, line in enumerate(lines, 1):
            if line and not line.startswith("#") and not line.startswith("extends"):
                leading = line[:len(line) - len(line.lstrip())]
                if leading and "\t" not in leading and " " in leading:
                    if line.lstrip().startswith(("func ", "var ", "const ", "if ", "elif ", "else", "for ", "while ", "match ", "return", "pass")):
                        errors.append(f"{filename} line {i}: Uses space indentation instead of tabs")
                        break  # Only report once

        return errors

    def _structural_validate(self, project_dir: str) -> list[str]:
        """Structural validation of project (check missing files, bad references, shapes, etc.)."""
        project_path = Path(project_dir)
        issues = []

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

        for tscn_file in project_path.rglob("*.tscn"):
            tscn_content = tscn_file.read_text(encoding="utf-8")
            rel = str(tscn_file.relative_to(project_path))
            lines = tscn_content.split("\n")

            # Check referenced scripts exist
            for line in lines:
                if 'type="Script"' in line and 'path="' in line:
                    path_start = line.index('path="') + 6
                    path_end = line.index('"', path_start)
                    script_path = line[path_start:path_end].replace("res://", "")
                    if not (project_path / script_path).exists():
                        issues.append(f"{rel}: Missing script reference {script_path}")

            # Check for invalid node types
            for line in lines:
                if line.startswith("[node "):
                    type_match = re.search(r'type="(\w+)"', line)
                    if type_match and type_match.group(1) == "PackedScene":
                        name_match = re.search(r'name="(\w+)"', line)
                        node_name = name_match.group(1) if name_match else "unknown"
                        issues.append(f"{rel}: Node '{node_name}' uses invalid type 'PackedScene' - use instance=ExtResource() syntax")

            # Check for CollisionShape nodes without shape assigned
            collision_nodes = set()
            collision_with_shape = set()
            current_node_name = None
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
                issues.append(f"{rel}: CollisionShape '{node_name}' has no shape - must assign a SubResource (e.g. RectangleShape2D, CircleShape2D)")

            # Check unquoted text values
            for i, line in enumerate(lines, 1):
                if line.startswith("text = ") and not line.startswith("text = \""):
                    val = line.split("=", 1)[1].strip()
                    if val and not val.replace(".", "").replace("-", "").isdigit():
                        if not val.startswith(("SubResource", "ExtResource")):
                            issues.append(f"{rel} line {i}: text value should be quoted: text = \"{val}\"")

        return issues

    def _tool_read_file(self, args: dict) -> str:
        """Tool: read_file - read a project file."""
        filename = args.get("filename", "")
        if not self._output_dir:
            return json.dumps({"ok": False, "error": "No project generated yet"})

        filepath = self._safe_project_path(filename)
        if filepath is None:
            return json.dumps({"ok": False, "error": f"Invalid path: {filename}"})
        if not filepath.exists():
            return json.dumps({"ok": False, "error": f"File not found: {filename}"})

        try:
            content = filepath.read_text(encoding="utf-8")
            self._mark_file_read(filename)
            # Add line numbers for easier patch_file usage
            numbered = "\n".join(
                f"{i+1:4d}: {line}"
                for i, line in enumerate(content.split("\n"))
            )
            return json.dumps({"ok": True, "filename": filename, "content": content, "numbered": numbered})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def _tool_write_file(self, args: dict) -> str:
        """Tool: write_file - write/overwrite a project file."""
        filename = args.get("filename", "")
        content = args.get("content", "")
        if not self._output_dir:
            return json.dumps({"ok": False, "error": "No project generated yet"})

        filepath = self._safe_project_path(filename)
        if filepath is None:
            return json.dumps({"ok": False, "error": f"Invalid path: {filename}"})

        # Safety: warn if overwriting existing file without reading it first
        warning = ""
        if filepath.exists() and not self._was_file_read(filename):
            try:
                old_content = filepath.read_text(encoding="utf-8")
                old_lines = len(old_content.splitlines())
                new_lines = len(content.splitlines())
                if old_lines > 20 and new_lines < old_lines * 0.7:
                    warning = (
                        f"WARNING: You are overwriting {filename} ({old_lines} lines) with only {new_lines} lines "
                        f"without reading it first. You may be losing code. Consider using read_file + patch_file instead."
                    )
            except Exception:
                pass

        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(filepath, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            msg = f"File '{filename}' written successfully."
            if warning:
                msg += f" {warning}"
            return json.dumps({"ok": True, "message": msg, "warning": warning or None})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def _tool_list_files(self, args: dict) -> str:
        """Tool: list_files - list project files."""
        if not self._output_dir:
            return json.dumps({"ok": False, "error": "No project generated yet"})

        project_path = Path(self._output_dir)
        if not project_path.exists():
            return json.dumps({"ok": False, "error": "Project directory not found"})

        files = []
        for f in sorted(project_path.rglob("*")):
            if not f.is_file():
                continue
            rel = str(f.relative_to(project_path)).replace("\\", "/")
            if is_ignored_path(rel):
                continue
            files.append(rel)

        return json.dumps({"ok": True, "files": files})

    def _tool_list_assets(self, args: dict) -> str:
        """Tool: list_assets - list uploaded asset files (images, audio, 3D models, fonts)."""
        if not self._output_dir:
            return json.dumps({"ok": True, "assets": [], "message": "No project directory set yet. Assets uploaded by the user will appear here."})

        project_path = Path(self._output_dir)
        if not project_path.exists():
            return json.dumps({"ok": True, "assets": [], "message": "Project directory doesn't exist yet."})

        assets = []
        for f in sorted(project_path.rglob("*")):
            if not f.is_file():
                continue
            rel = str(f.relative_to(project_path)).replace("\\", "/")
            if is_ignored_path(rel):
                continue
            if f.suffix.lower() in ASSET_EXTENSIONS_ALL:
                category = classify_asset(f.suffix)
                asset_info: dict[str, Any] = {
                    "filename": f.name,
                    "path": rel,
                    "res_path": f"res://{rel}",
                    "category": category,
                    "size_bytes": f.stat().st_size,
                }
                # Analyze images for sprite sheet detection
                if category == "image" and f.suffix.lower() in ASSET_EXTENSIONS_IMAGE:
                    meta = _analyze_sprite_sheet(f)
                    if meta:
                        asset_info["width"] = meta["width"]
                        asset_info["height"] = meta["height"]
                        if meta.get("is_sheet"):
                            # Use vision AI to identify animation row names
                            meta = _analyze_sprite_rows_with_vision(f, meta)
                            asset_info["is_sprite_sheet"] = True
                            asset_info["hframes"] = meta["hframes"]
                            asset_info["vframes"] = meta["vframes"]
                            asset_info["frame_width"] = meta["frame_width"]
                            asset_info["frame_height"] = meta["frame_height"]
                            asset_info["total_frames"] = meta["total_frames"]
                            asset_info["detection_confidence"] = "high" if meta.get("detection_method") == "alpha_scan" else "low"
                            if meta.get("vision_analyzed"):
                                asset_info["animation_names_source"] = "vision_ai"
                            if meta.get("rows_info"):
                                asset_info["animation_rows"] = meta["rows_info"]
                # Analyze 3D models for mesh/animation metadata
                elif category == "model" and f.suffix.lower() in ASSET_EXTENSIONS_MODEL:
                    meta_3d = _analyze_3d_model(f)
                    if meta_3d:
                        asset_info["model_meta"] = meta_3d
                assets.append(asset_info)

        if assets:
            # Build detailed usage instructions per category
            usage_hints = []
            images = [a for a in assets if a["category"] == "image"]
            audios = [a for a in assets if a["category"] == "audio"]
            models = [a for a in assets if a["category"] == "model"]
            fonts = [a for a in assets if a["category"] == "font"]
            
            if images:
                img_lines = []
                for a in images:
                    if a.get("is_sprite_sheet"):
                        confidence = a.get("detection_confidence", "unknown")
                        names_source = a.get("animation_names_source", "generic")
                        img_lines.append(
                            f"  {a['filename']}: SPRITE SHEET ({a['width']}×{a['height']}px, "
                            f"grid {a['hframes']}col × {a['vframes']}row, frame {a['frame_width']}×{a['frame_height']}px, "
                            f"grid detection: {confidence})\n"
                            f"    → USE: Sprite2D(hframes={a['hframes']}, vframes={a['vframes']}) "
                            f"OR AnimatedSprite2D + SpriteFrames + AtlasTexture(region=Rect2(col*{a['frame_width']}, row*{a['frame_height']}, {a['frame_width']}, {a['frame_height']}))\n"
                            f"    ⚠️ DO NOT use as single texture!"
                        )
                        if a.get("animation_rows"):
                            for ri in a["animation_rows"][:8]:
                                source_tag = " [AI]" if ri.get("name_source") == "vision_ai" else " [unverified]"
                                img_lines.append(f"    Row {ri['row']} ({ri['frame_count']} frames): \"{ri['suggested_name']}\"{source_tag}")
                        if names_source != "vision_ai":
                            img_lines.append(f"    ⚠️ Animation names are auto-guessed — ask user to confirm if unsure")
                    else:
                        w = a.get("width", "?")
                        h = a.get("height", "?")
                        img_lines.append(f"  {a['filename']}: Single image ({w}×{h}px)")
                usage_hints.append(
                    f"IMAGES ({len(images)}):\n" + "\n".join(img_lines) + "\n"
                    f"  preload usage: sprite.texture = preload(\"{images[0]['res_path']}\")"
                )
            if audios:
                names = ", ".join(a["filename"] for a in audios)
                usage_hints.append(
                    f"AUDIO ({len(audios)}): {names}\n"
                    f"  In GDScript: var sfx = preload(\"{audios[0]['res_path']}\")\n"
                    f"  For AudioStreamPlayer: $AudioPlayer.stream = preload(\"{audios[0]['res_path']}\")\n"
                    f"  Play: $AudioPlayer.play()"
                )
            if models:
                model_lines = []
                for a in models:
                    meta_3d = a.get("model_meta")
                    if meta_3d:
                        model_lines.append(f"  {a['filename']}: {_format_3d_model_info(meta_3d, a['res_path'])}")
                    else:
                        model_lines.append(f"  {a['filename']}: 3D model (no metadata available)")
                        model_lines.append(f"    Instance in scene: use \"instance\": \"{a['res_path']}\" in node JSON")
                        model_lines.append(f"    GDScript: var m = preload(\"{a['res_path']}\").instantiate(); add_child(m)")
                usage_hints.append(
                    f"3D MODELS ({len(models)}):\n" + "\n".join(model_lines) + "\n"
                    f"  ⚠️ To instance in generate_project, add \"instance\": \"res://...\" to the node (NOT type=\"PackedScene\")"
                )
            if fonts:
                names = ", ".join(a["filename"] for a in fonts)
                usage_hints.append(
                    f"FONTS ({len(fonts)}): {names}\n"
                    f"  In GDScript: var font = preload(\"{fonts[0]['res_path']}\")\n"
                    f"  For Label: label.add_theme_font_override(\"font\", font)"
                )
            
            return json.dumps({
                "ok": True,
                "assets": assets,
                "message": (
                    f"Found {len(assets)} asset(s) uploaded by the user. "
                    f"YOU MUST USE THESE ASSETS in your project! Do NOT use ColorRect placeholders when real assets exist.\n\n"
                    f"USAGE GUIDE:\n" + "\n".join(usage_hints) + "\n\n"
                    f"CRITICAL: In your generate_project call, write GDScript code that preloads and uses these assets. "
                    f"After generate_project, verify with read_file that assets are actually referenced, and fix with write_file if not."
                )
            })
        else:
            return json.dumps({
                "ok": True,
                "assets": [],
                "message": "No asset files found. The user hasn't uploaded any images, audio, models, or fonts yet. Use ColorRect/primitives as placeholders."
            })


    # ─── Asset Helpers ───

    def _scan_assets_for_prompt(self) -> str | None:
        """Pre-scan project directory for assets and return a formatted string for system prompt injection.
        This ensures the LLM knows about assets from the VERY FIRST message, not just after list_assets.
        Includes sprite sheet analysis with frame grid metadata."""
        if not self._output_dir:
            return None
        project_path = Path(self._output_dir)
        if not project_path.exists():
            return None

        assets = []
        for f in sorted(project_path.rglob("*")):
            if not f.is_file():
                continue
            rel = str(f.relative_to(project_path)).replace("\\", "/")
            if is_ignored_path(rel):
                continue
            if f.suffix.lower() in ASSET_EXTENSIONS_ALL:
                category = classify_asset(f.suffix)
                asset_entry: dict[str, Any] = {"filename": f.name, "res_path": f"res://{rel}", "category": category, "file_path": f}
                # Analyze images for sprite sheet detection
                if category == "image" and f.suffix.lower() in ASSET_EXTENSIONS_IMAGE:
                    meta = _analyze_sprite_sheet(f)
                    if meta:
                        if meta.get("is_sheet"):
                            meta = _analyze_sprite_rows_with_vision(f, meta)
                        asset_entry["meta"] = meta
                # Analyze 3D models for mesh/animation metadata
                elif category == "model" and f.suffix.lower() in ASSET_EXTENSIONS_MODEL:
                    meta_3d = _analyze_3d_model(f)
                    if meta_3d:
                        asset_entry["meta_3d"] = meta_3d
                assets.append(asset_entry)

        if not assets:
            return None

        lines = []
        images = [a for a in assets if a["category"] == "image"]
        audios = [a for a in assets if a["category"] == "audio"]
        fonts = [a for a in assets if a["category"] == "font"]
        models = [a for a in assets if a["category"] == "model"]

        if images:
            lines.append("### Images:")
            for a in images:
                meta = a.get("meta")
                hint = ""
                name_low = a["filename"].lower()

                # Sprite sheet metadata takes priority
                if meta and meta.get("is_sheet"):
                    sheet_info = _format_sprite_sheet_info(meta, a["res_path"])
                    lines.append(f"- `{a['res_path']}` → {sheet_info}")
                    continue

                # Single image hints
                size_info = ""
                if meta:
                    size_info = f" ({meta['width']}×{meta['height']}px)"

                if "player" in name_low or "hero" in name_low or "character" in name_low:
                    hint = " → PLAYER sprite"
                elif "enemy" in name_low or "slime" in name_low or "monster" in name_low:
                    hint = " → ENEMY sprite"
                elif "bullet" in name_low or "projectile" in name_low:
                    hint = " → BULLET sprite"
                elif "background" in name_low or "bg" in name_low or "forest" in name_low:
                    hint = " → BACKGROUND"
                elif "foxy" in name_low or "fox" in name_low:
                    hint = " → CHARACTER sprite (Foxy)"
                lines.append(f"- `{a['res_path']}`{size_info}{hint}")
            lines.append(f"  Usage: `sprite.texture = preload(\"{images[0]['res_path']}\")`")

        if audios:
            lines.append("### Audio (use for AudioStreamPlayer/2D/3D):")
            for a in audios:
                hint = ""
                name_low = a["filename"].lower()
                if "bgm" in name_low or "music" in name_low:
                    hint = " → BACKGROUND MUSIC (loop)"
                elif "death" in name_low or "die" in name_low:
                    hint = " → DEATH sound effect"
                elif "gun" in name_low or "shoot" in name_low or "fire" in name_low:
                    hint = " → SHOOTING sound effect"
                elif "gameover" in name_low or "game_over" in name_low:
                    hint = " → GAME OVER sound"
                elif "running" in name_low or "walk" in name_low or "step" in name_low:
                    hint = " → FOOTSTEP/RUNNING sound"
                lines.append(f"- `{a['res_path']}`{hint}")
            lines.append(f"  Usage: `$AudioPlayer.stream = preload(\"{audios[0]['res_path']}\")`; `$AudioPlayer.play()`")

        if fonts:
            lines.append("### Fonts (use for Label, RichTextLabel, HUD text):")
            for a in fonts:
                lines.append(f"- `{a['res_path']}`")
            lines.append(f"  Usage: `$Label.add_theme_font_override(\"font\", preload(\"{fonts[0]['res_path']}\"))`")

        if models:
            lines.append("### 3D Models (use \"instance\" field in generate_project nodes, or preload in GDScript):")
            for a in models:
                meta_3d = a.get("meta_3d")
                if meta_3d:
                    info = _format_3d_model_info(meta_3d, a["res_path"])
                    lines.append(f"- `{a['res_path']}` → {info}")
                else:
                    lines.append(f"- `{a['res_path']}`")
            lines.append(f"  Instance in scene JSON: `\"instance\": \"{models[0]['res_path']}\"`")
            lines.append(f"  GDScript: `var m = preload(\"{models[0]['res_path']}\").instantiate(); add_child(m)`")

        lines.append(f"\n**Total: {len(assets)} assets. You MUST preload() and use ALL of them in your scripts.**")
        return "\n".join(lines)

    def _verify_assets_in_scripts(self) -> list[str]:
        """After project generation, verify that scripts actually reference uploaded assets.
        Returns a list of missing asset references."""
        if not self._output_dir:
            return []
        project_path = Path(self._output_dir)

        # Collect asset res:// paths
        asset_res_paths: list[str] = []
        for f in project_path.rglob("*"):
            if not f.is_file():
                continue
            rel = str(f.relative_to(project_path)).replace("\\", "/")
            if is_ignored_path(rel):
                continue
            if f.suffix.lower() in ASSET_EXTENSIONS_ALL:
                asset_res_paths.append(f"res://{rel}")

        if not asset_res_paths:
            return []

        # Read all .gd and .tscn files to check references
        all_code = ""
        for f in project_path.rglob("*"):
            if f.is_file() and f.suffix in (".gd", ".tscn"):
                try:
                    all_code += f.read_text(encoding="utf-8") + "\n"
                except Exception:
                    pass

        missing = []
        for res_path in asset_res_paths:
            # Check if the res_path OR the filename appears in any script/scene
            filename = res_path.split("/")[-1]
            if res_path not in all_code and filename not in all_code:
                missing.append(res_path)

        return missing

    # ─── Tool Dispatch ───

    def _tool_delete_file(self, args: dict) -> str:
        """Tool: delete_file - remove a file from the project."""
        filename = args.get("filename", "")
        if not filename or not self._output_dir:
            return json.dumps({"ok": False, "error": "Missing filename or project dir"})
        filepath = self._safe_project_path(filename)
        if filepath is None:
            return json.dumps({"ok": False, "error": f"Invalid path: {filename}"})
        if not filepath.exists():
            return json.dumps({"ok": False, "error": f"File not found: {filename}"})
        try:
            # Safety: don't delete project.godot
            if filepath.name == "project.godot":
                return json.dumps({"ok": False, "error": "Cannot delete project.godot"})
            filepath.unlink()
            self._add_step("success", f"Deleted {filename}")
            return json.dumps({"ok": True, "message": f"Deleted {filename}"})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def _tool_rename_file(self, args: dict) -> str:
        """Tool: rename_file - rename/move a file and update references."""
        old_name = args.get("old_filename", "")
        new_name = args.get("new_filename", "")
        if not old_name or not new_name or not self._output_dir:
            return json.dumps({"ok": False, "error": "Missing old_filename, new_filename, or project dir"})
        old_path = self._safe_project_path(old_name)
        new_path = self._safe_project_path(new_name)
        if old_path is None or new_path is None:
            return json.dumps({"ok": False, "error": "Invalid path (path traversal not allowed)"})
        if not old_path.exists():
            return json.dumps({"ok": False, "error": f"File not found: {old_name}"})
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.rename(new_path)
            # Update references in .tscn and .gd files
            old_res = f"res://{old_name}"
            new_res = f"res://{new_name}"
            updated_refs = []
            project_dir = Path(self._output_dir)
            for f in project_dir.rglob("*"):
                if f.is_file() and f.suffix in (".tscn", ".gd", ".tres", ".cfg") and f != new_path:
                    try:
                        content = f.read_text(encoding="utf-8")
                        if old_res in content or old_name in content:
                            content = content.replace(old_res, new_res)
                            content = content.replace(f'"{old_name}"', f'"{new_name}"')
                            f.write_text(content, encoding="utf-8", newline="\n")
                            updated_refs.append(str(f.relative_to(project_dir)))
                    except Exception:
                        pass
            msg = f"Renamed {old_name} → {new_name}"
            if updated_refs:
                msg += f". Updated references in: {', '.join(updated_refs)}"
            self._add_step("success", msg)
            return json.dumps({"ok": True, "message": msg, "updated_references": updated_refs})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def _tool_search_files(self, args: dict) -> str:
        """Tool: search_files - search for text/pattern across project files."""
        import re as _re
        pattern = args.get("pattern", "")
        ext_filter = args.get("file_extensions", "")
        if not pattern or not self._output_dir:
            return json.dumps({"ok": False, "error": "Missing pattern or project dir"})
        
        extensions = [e.strip() for e in ext_filter.split(",") if e.strip()] if ext_filter else []
        project_dir = Path(self._output_dir)
        results = []
        try:
            regex = _re.compile(pattern, _re.IGNORECASE)
        except _re.error:
            regex = None
        
        for f in sorted(project_dir.rglob("*")):
            if not f.is_file() or f.name.startswith(".") or ".godot" in str(f):
                continue
            if extensions and f.suffix not in extensions:
                continue
            try:
                content = f.read_text(encoding="utf-8")
                rel = str(f.relative_to(project_dir)).replace("\\", "/")
                for i, line in enumerate(content.splitlines(), 1):
                    matched = (regex.search(line) if regex else pattern.lower() in line.lower())
                    if matched:
                        results.append({"file": rel, "line": i, "text": line.rstrip()})
                        if len(results) >= 100:
                            break
            except Exception:
                pass
            if len(results) >= 100:
                break
        
        self._add_step("thinking", f"Search '{pattern}': {len(results)} match(es)")
        return json.dumps({"ok": True, "pattern": pattern, "matches": results, "total": len(results)})

    def _tool_edit_project_settings(self, args: dict) -> str:
        """Tool: edit_project_settings - modify project.godot settings."""
        if not self._output_dir:
            return json.dumps({"ok": False, "error": "No project directory set"})
        
        project_godot = Path(self._output_dir) / "project.godot"
        if not project_godot.exists():
            return json.dumps({"ok": False, "error": "project.godot not found"})
        
        try:
            content = project_godot.read_text(encoding="utf-8")
            
            # Handle raw_append — just append text to the end
            raw_append = args.get("raw_append", "")
            if raw_append:
                if not content.endswith("\n"):
                    content += "\n"
                content += "\n" + raw_append.strip() + "\n"
            
            # Handle structured settings — find or create sections
            settings = args.get("settings", {})
            if settings:
                for key, value in settings.items():
                    # Key format: "section/subsection/property" or "input/action_name"
                    # Convert to project.godot format
                    parts = key.split("/", 1)
                    if len(parts) == 2:
                        section = parts[0]
                        prop = parts[1]
                    else:
                        section = "application"
                        prop = key
                    
                    section_header = f"[{section}]"
                    
                    # Format value based on type and context
                    if section == "input" and isinstance(value, dict):
                        # Input mapping: format as Godot input action
                        val_str = self._format_input_action(prop, value)
                    elif isinstance(value, bool):
                        val_str = "true" if value else "false"
                    elif isinstance(value, (int, float)):
                        val_str = str(value)
                    elif isinstance(value, str):
                        val_str = f'"{value}"'
                    elif isinstance(value, dict):
                        # Generic dict — convert Python bools/None to Godot format
                        val_str = self._format_godot_value(value)
                    else:
                        val_str = str(value)
                    
                    # For input actions, val_str already includes the full multi-line block
                    if section == "input" and isinstance(value, dict):
                        line_entry = val_str  # Already formatted as "prop={\n...\n}"
                    else:
                        line_entry = f"{prop}={val_str}"
                    
                    if section_header in content:
                        # Find the section and check if property exists
                        lines = content.split("\n")
                        section_idx = None
                        prop_idx = None
                        prop_end_idx = None
                        next_section_idx = None
                        for i, line in enumerate(lines):
                            if line.strip() == section_header:
                                section_idx = i
                            elif section_idx is not None and line.strip().startswith("[") and line.strip().endswith("]"):
                                next_section_idx = i
                                break
                            elif section_idx is not None and line.startswith(f"{prop}="):
                                prop_idx = i
                                # For multi-line values (like input actions with {}), find the end
                                if "{" in line:
                                    brace_count = line.count("{") - line.count("}")
                                    prop_end_idx = i
                                    while brace_count > 0 and prop_end_idx + 1 < len(lines):
                                        prop_end_idx += 1
                                        brace_count += lines[prop_end_idx].count("{") - lines[prop_end_idx].count("}")
                                else:
                                    prop_end_idx = i
                        
                        if prop_idx is not None:
                            # Replace existing property (possibly multi-line)
                            lines[prop_idx:prop_end_idx + 1] = line_entry.split("\n")
                        elif section_idx is not None:
                            insert_at = next_section_idx if next_section_idx else len(lines)
                            for j, new_line in enumerate(line_entry.split("\n")):
                                lines.insert(insert_at + j, new_line)
                        content = "\n".join(lines)
                    else:
                        # Add new section
                        if not content.endswith("\n"):
                            content += "\n"
                        content += f"\n{section_header}\n\n{line_entry}\n"
            
            project_godot.write_text(content, encoding="utf-8", newline="\n")
            self._add_step("success", "Updated project.godot settings")
            return json.dumps({"ok": True, "message": "project.godot updated"})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    @staticmethod
    def _format_input_action(action_name: str, action_data: dict) -> str:
        """Format an input action dict into Godot project.godot format.
        
        Converts LLM-provided input action data (Python dict) into Godot's 
        native Object() serialization format for project.godot.
        """
        deadzone = action_data.get("deadzone", 0.2)
        events = action_data.get("events", [])
        
        formatted_events = []
        for evt in events:
            evt_type = evt.get("type", "InputEventKey")
            if evt_type == "InputEventKey":
                keycode = evt.get("physical_keycode", evt.get("keycode", 0))
                formatted_events.append(
                    f'Object(InputEventKey,"resource_local_to_scene":false,"resource_name":"",'
                    f'"device":-1,"window_id":0,"alt_pressed":false,"shift_pressed":false,'
                    f'"ctrl_pressed":false,"meta_pressed":false,"pressed":false,"keycode":0,'
                    f'"physical_keycode":{keycode},"key_label":0,"unicode":0,"location":0,'
                    f'"echo":false,"script":null)'
                )
            elif evt_type == "InputEventJoypadButton":
                button = evt.get("button_index", 0)
                formatted_events.append(
                    f'Object(InputEventJoypadButton,"resource_local_to_scene":false,'
                    f'"resource_name":"","device":-1,"button_index":{button},'
                    f'"pressure":0.0,"pressed":false,"script":null)'
                )
            elif evt_type == "InputEventJoypadMotion":
                axis = evt.get("axis", 0)
                axis_value = evt.get("axis_value", 1.0)
                formatted_events.append(
                    f'Object(InputEventJoypadMotion,"resource_local_to_scene":false,'
                    f'"resource_name":"","device":-1,"axis":{axis},'
                    f'"axis_value":{axis_value},"script":null)'
                )
            elif evt_type == "InputEventMouseButton":
                button = evt.get("button_index", 1)
                formatted_events.append(
                    f'Object(InputEventMouseButton,"resource_local_to_scene":false,'
                    f'"resource_name":"","device":-1,"window_id":0,"alt_pressed":false,'
                    f'"shift_pressed":false,"ctrl_pressed":false,"meta_pressed":false,'
                    f'"button_mask":0,"position":Vector2(0,0),"global_position":Vector2(0,0),'
                    f'"factor":1.0,"button_index":{button},"canceled":false,"pressed":false,'
                    f'"double_click":false,"script":null)'
                )
        
        events_str = ", ".join(formatted_events)
        return (
            f'{action_name}={{\n'
            f'"deadzone": {deadzone},\n'
            f'"events": [{events_str}]\n'
            f'}}'
        )

    @staticmethod
    def _format_godot_value(value: Any) -> str:
        """Convert a Python value to Godot config format string."""
        if isinstance(value, bool):
            return "true" if value else "false"
        elif isinstance(value, (int, float)):
            return str(value)
        elif isinstance(value, str):
            return f'"{value}"'
        elif value is None:
            return "null"
        elif isinstance(value, dict):
            # Convert Python dict to Godot-compatible string
            parts = []
            for k, v in value.items():
                parts.append(f'"{k}": {GameGenerator._format_godot_value(v)}')
            return "{" + ", ".join(parts) + "}"
        elif isinstance(value, list):
            items = [GameGenerator._format_godot_value(v) for v in value]
            return "[" + ", ".join(items) + "]"
        return str(value)

    def _tool_run_project(self, args: dict) -> str:
        """Tool: run_project - run the game and capture output/errors."""
        if not self._output_dir:
            return json.dumps({"ok": False, "error": "No project directory set"})
        
        timeout = args.get("timeout_seconds", 5)
        if timeout < 1:
            timeout = 1
        if timeout > 30:
            timeout = 30
        
        try:
            if not self._runner or not self._runner.is_available():
                return json.dumps({
                    "ok": False,
                    "error": (
                        "Godot executable not available. Cannot run project. "
                        "DO NOT ask the user to run the project and send you output. "
                        "Instead: (1) Use `read_file` to inspect the code and reason about bugs. "
                        "(2) Use `validate_project` for static syntax checking. "
                        "(3) For 3D animations, ALWAYS use runtime discovery patterns (get_animation_list + keyword matching) — never hardcode animation names. "
                        "(4) Fix issues based on code analysis alone."
                    )
                })
            
            import subprocess
            import time as _time
            
            exe = str(self._runner.exe_path)
            project_path = str(Path(self._output_dir).resolve())
            
            self._add_step("thinking", f"Running project for {timeout}s...")
            
            # Ensure resources are imported first (GLB models, textures, etc.)
            import_proc = subprocess.run(
                [exe, "--path", project_path, "--headless", "--import"],
                capture_output=True,
                timeout=60,
                cwd=project_path,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if import_proc.returncode != 0:
                logger.warning("Godot --import returned %d", import_proc.returncode)
            
            proc = subprocess.Popen(
                [exe, "--path", project_path, "--headless", "--quit-after", str(timeout)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=project_path,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            
            try:
                stdout, stderr = proc.communicate(timeout=timeout + 10)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            
            stdout_text = stdout.decode("utf-8", errors="replace")[-3000:]  # Last 3000 chars
            stderr_text = stderr.decode("utf-8", errors="replace")[-3000:]
            
            # Parse for errors
            errors = []
            for line in (stdout_text + stderr_text).splitlines():
                low = line.lower()
                if any(kw in low for kw in ["error", "exception", "failed", "invalid", "crash"]):
                    errors.append(line.strip())
            
            has_errors = len(errors) > 0
            self._add_step(
                "error" if has_errors else "success",
                f"Project run {'had errors' if has_errors else 'OK'} (exit code {proc.returncode})"
            )
            
            return json.dumps({
                "ok": not has_errors,
                "exit_code": proc.returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "errors": errors[:20],
                "message": f"Ran for {timeout}s. {'Errors found.' if has_errors else 'No errors detected.'}"
            })
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def _tool_export_web(self, args: dict) -> str:
        """Tool: export_web - export project as HTML5/Web for browser play."""
        if not self._output_dir:
            return json.dumps({"ok": False, "error": "No project directory set"})

        if not self._runner or not self._runner.is_available():
            return json.dumps({
                "ok": False,
                "error": "Godot executable not available. Cannot export project."
            })

        self._add_step("thinking", "Exporting project for Web (HTML5)...")

        result = self._runner.export_project_web(self._output_dir)

        if result["ok"]:
            project_name = Path(self._output_dir).name
            play_url = f"/play/{project_name}/"
            self._add_step("success", f"Web export complete! Play at: {play_url}")
            result["play_url"] = play_url
        else:
            self._add_step("error", f"Web export failed: {result.get('error', 'Unknown error')}")

        return json.dumps(result)

    def _tool_create_resource(self, args: dict) -> str:
        """Tool: create_resource - create a .tres resource file."""
        filename = args.get("filename", "")
        content = args.get("content", "")
        if not filename or not content or not self._output_dir:
            return json.dumps({"ok": False, "error": "Missing filename, content, or project dir"})
        
        if not filename.endswith(".tres"):
            return json.dumps({"ok": False, "error": "Resource files must have .tres extension"})
        
        filepath = self._safe_project_path(filename)
        if filepath is None:
            return json.dumps({"ok": False, "error": f"Invalid path: {filename}"})
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(filepath, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            self._add_step("success", f"Created resource: {filename}")
            return json.dumps({"ok": True, "message": f"Created {filename}"})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def _tool_patch_file(self, args: dict) -> str:
        """Tool: patch_file - make targeted search-replace edits to a file."""
        filename = args.get("filename", "")
        patches = args.get("patches", [])
        if not filename or not patches or not self._output_dir:
            return json.dumps({"ok": False, "error": "Missing filename, patches, or project dir"})
        
        filepath = self._safe_project_path(filename)
        if filepath is None:
            return json.dumps({"ok": False, "error": f"Invalid path: {filename}"})
        if not filepath.exists():
            return json.dumps({"ok": False, "error": f"File not found: {filename}"})
        
        try:
            content = filepath.read_text(encoding="utf-8")
            applied = []
            failed = []
            
            for i, patch in enumerate(patches):
                old = patch.get("old", "")
                new = patch.get("new", "")
                if not old:
                    failed.append(f"Patch {i}: empty 'old' string")
                    continue
                if old in content:
                    content = content.replace(old, new, 1)
                    applied.append(f"Patch {i}: replaced {len(old)} chars")
                else:
                    # Give LLM the FULL old string it tried (not truncated) + nearby context
                    # Find best partial match to help LLM understand what went wrong
                    hint = ""
                    old_first_line = old.split("\n")[0].strip()
                    if old_first_line:
                        for j, line in enumerate(content.splitlines()):
                            if old_first_line in line:
                                start = max(0, j - 1)
                                end = min(len(content.splitlines()), j + 5)
                                context_lines = content.splitlines()[start:end]
                                hint = f" | Closest match near line {j+1}:\n" + "\n".join(
                                    f"  {start+k+1}: {ln}" for k, ln in enumerate(context_lines)
                                )
                                break
                    failed.append(
                        f"Patch {i}: NOT FOUND in file. Your 'old' text does not exactly match the file content.{hint}\n"
                        f"  Your 'old' was: {repr(old)}"
                    )
            
            if applied:
                with open(filepath, "w", encoding="utf-8", newline="\n") as f:
                    f.write(content)
            
            success = len(applied) > 0
            msg = f"Applied {len(applied)}/{len(patches)} patches"
            result_data: dict[str, Any] = {
                "ok": success,
                "applied": len(applied),
                "failed": failed,
                "message": msg,
            }
            if failed:
                msg += f". FAILED patches: {len(failed)}"
                result_data["message"] = msg
                # If ANY patch failed, include the current file content so LLM can see the real state
                result_data["current_file_content"] = content
                result_data["fix_hint"] = (
                    "Some patches failed because the 'old' text didn't match the file. "
                    "The current file content is included above. "
                    "Use the EXACT text from the file for your next patch_file call, or use write_file to rewrite the whole file."
                )
            self._add_step("success" if success else "error", msg)
            return json.dumps(result_data)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def _tool_update_memory(self, args: dict) -> str:
        """Tool: update_memory - save project context to memory.md with size enforcement."""
        content = args.get("content", "")
        if not self._output_dir:
            return json.dumps({"ok": False, "error": "No project directory set"})

        # Hard limit: truncate if LLM ignores the 2000-char instruction
        MAX_MEMORY_CHARS = 8000  # generous hard cap (~2000 tokens)
        was_truncated = False
        if len(content) > MAX_MEMORY_CHARS:
            # Keep the beginning (overview/architecture) and trim the end (old changelog)
            content = content[:MAX_MEMORY_CHARS]
            # Find last complete line to avoid mid-line cut
            last_newline = content.rfind("\n")
            if last_newline > MAX_MEMORY_CHARS * 0.8:
                content = content[:last_newline]
            content += "\n\n<!-- Truncated: memory exceeded size limit. Keep it concise! -->\n"
            was_truncated = True

        try:
            memory_path = Path(self._output_dir) / "memory.md"
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            with open(memory_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            msg = f"Saved {len(content)} chars to memory.md"
            if was_truncated:
                msg += " (truncated from original — please keep memory under 2000 chars next time)"
            self._add_step("success", "Project memory updated", msg)
            return json.dumps({"ok": True, "message": msg})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def _tool_analyze_image(self, args: dict) -> str:
        """Tool: analyze_image — use Gemini Flash vision to analyze a user-attached image.

        The main Agent (Claude Opus) cannot see images directly. It calls this tool
        with a specific prompt to get a text description/analysis of the image.
        """
        image_index = args.get("image_index", 1)
        prompt = args.get("prompt", "Describe this image in detail.")

        if not self._images:
            return json.dumps({"ok": False, "error": "No images attached to this message."})

        if image_index < 1 or image_index > len(self._images):
            return json.dumps({
                "ok": False,
                "error": f"Invalid image_index {image_index}. Available: 1 to {len(self._images)}."
            })

        image_data = self._images[image_index - 1]
        self._add_step("tool_call", f"Analyzing image {image_index} with vision AI...")

        result = _analyze_image_with_vision(image_data, prompt)

        self._add_step("success", f"Image {image_index} analysis complete")
        return json.dumps({"ok": True, "image_index": image_index, "analysis": result})

    def _load_project_memory(self) -> str | None:
        """Load project memory from memory.md if it exists."""
        if not self._output_dir:
            return None
        memory_path = Path(self._output_dir) / "memory.md"
        if memory_path.exists():
            try:
                return memory_path.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    def _prepare_memory_for_injection(self, memory_content: str) -> str:
        """Prepare memory for injection: summarize if too long using LLM.

        If memory exceeds MAX_MEMORY_TOKENS, use a single LLM call to compress
        it into a concise ~500-word summary. This prevents memory bloat from
        degrading the agent's ability to focus on the current request.
        """
        memory_tokens = _estimate_tokens(memory_content)

        if memory_tokens <= MAX_MEMORY_TOKENS:
            return memory_content

        # Memory is too long — try LLM summarization
        self._add_step("thinking",
                       f"Memory too long (~{memory_tokens} tokens > {MAX_MEMORY_TOKENS}), summarizing...")
        try:
            summary_messages = [
                {"role": "system", "content": (
                    "You are a concise technical summarizer. Compress the following project memory "
                    "into a brief summary under 500 words. Rules:\n"
                    "- Keep: project name, game type, key architecture (main files/scenes)\n"
                    "- Keep: ONLY unresolved known issues\n"
                    "- Keep: last 3 changelog entries\n"
                    "- Remove: resolved bugs, redundant details, verbose descriptions\n"
                    "- Use the same markdown section headers (## Project Overview, ## Architecture, etc.)\n"
                    "- Output ONLY the compressed memory, no explanation"
                )},
                {"role": "user", "content": f"Compress this project memory:\n\n{memory_content}"},
            ]
            summary = self.llm.invoke(summary_messages, temperature=0.1, max_tokens=2048)
            if summary and summary.strip():
                compressed = summary.strip()
                new_tokens = _estimate_tokens(compressed)
                self._add_step("success",
                               f"Memory compressed: {memory_tokens} → {new_tokens} tokens")
                return compressed
        except Exception as e:
            logger.warning("Memory summarization failed: %s", e)

        # Fallback: hard truncate if LLM summarization fails
        max_chars = MAX_MEMORY_TOKENS * 4  # ~4 chars per token
        truncated = memory_content[:max_chars]
        last_newline = truncated.rfind("\n")
        if last_newline > max_chars * 0.7:
            truncated = truncated[:last_newline]
        truncated += "\n\n<!-- Memory truncated due to size. Older entries removed. -->"
        self._add_step("thinking", "LLM summarization failed, using hard truncation")
        return truncated

    @staticmethod
    def _deduplicate_history_against_memory(
        chat_history: list[dict[str, Any]],
        memory_content: str,
    ) -> list[dict[str, Any]]:
        """Remove chat history messages whose content substantially overlaps with memory.

        Checks if >60% of a message's significant words already appear in the memory.
        This prevents injecting redundant context that wastes tokens and confuses the LLM.
        """
        if not memory_content:
            return chat_history

        # Build set of significant words from memory (skip short/common words)
        memory_words = set()
        for word in memory_content.lower().split():
            if len(word) > 3:  # Skip short words like "the", "and", "is"
                memory_words.add(word.strip(".,;:!?()[]{}\"'`#*-_"))

        if not memory_words:
            return chat_history

        filtered = []
        for msg in chat_history:
            content = msg.get("content", "")
            if not isinstance(content, str) or not content.strip():
                filtered.append(msg)
                continue

            # Calculate overlap ratio
            msg_words = set()
            for word in content.lower().split():
                if len(word) > 3:
                    msg_words.add(word.strip(".,;:!?()[]{}\"'`#*-_"))

            if not msg_words:
                filtered.append(msg)
                continue

            overlap = len(msg_words & memory_words) / len(msg_words)

            if overlap < 0.6:
                # Less than 60% overlap — keep it, it has unique information
                filtered.append(msg)
            # else: skip this message — it's mostly duplicated in memory

        return filtered

    def _is_existing_project(self) -> bool:
        """Check if the output directory already has a Godot project."""
        if not self._output_dir:
            return False
        project_godot = Path(self._output_dir) / "project.godot"
        return project_godot.exists()

    def _collect_project_files(self) -> dict[str, str]:
        """Read all project source files and return {rel_path: content}."""
        files: dict[str, str] = {}
        if not self._output_dir:
            return files
        out_path = Path(self._output_dir)
        if not out_path.exists():
            return files
        for fpath in out_path.rglob("*"):
            if not fpath.is_file():
                continue
            rel = str(fpath.relative_to(out_path)).replace("\\", "/")
            if is_ignored_path(rel):
                continue
            if fpath.suffix in PROJECT_SOURCE_EXTENSIONS:
                try:
                    files[rel] = fpath.read_text(encoding="utf-8")
                except Exception:
                    pass
        return files

    def _build_project_info(self) -> dict[str, Any] | None:
        """Build project metadata dict from the last plan or filesystem."""
        if self._last_plan:
            return {
                "name": self._last_plan.name,
                "game_type": self._last_plan.game_type.value,
                "output_dir": self._output_dir,
                "scenes": [s.filename for s in self._last_plan.scenes],
                "scripts": [s.filename for s in self._last_plan.scripts],
            }
        if self._output_dir and self._is_existing_project():
            project_name = Path(self._output_dir).name
            scenes = [
                str(f.relative_to(self._output_dir)).replace("\\", "/")
                for f in Path(self._output_dir).rglob("*.tscn")
            ]
            scripts = [
                str(f.relative_to(self._output_dir)).replace("\\", "/")
                for f in Path(self._output_dir).rglob("*.gd")
            ]
            return {
                "name": project_name,
                "game_type": "unknown",
                "output_dir": self._output_dir,
                "scenes": scenes,
                "scripts": scripts,
            }
        return None

    def _build_tool_summary(self) -> str:
        """Build a compact summary of tool calls from the current agent loop for chat history."""
        tool_actions: list[str] = []
        for step in self._steps:
            if step.step_type == "tool_call":
                # Extract tool name from description like "Calling tool: write_file"
                name = step.description.replace("Calling tool: ", "")
                # Get key info from details
                detail_short = ""
                if step.details:
                    try:
                        d = json.loads(step.details)
                        if "filename" in d:
                            detail_short = f"({d['filename']})"
                        elif "name" in d:
                            detail_short = f"({d['name']})"
                        elif "pattern" in d:
                            detail_short = f"('{d['pattern']}')"
                    except (json.JSONDecodeError, TypeError):
                        pass
                tool_actions.append(f"{name}{detail_short}")
            elif step.step_type == "error":
                tool_actions.append(f"ERROR: {step.description[:80]}")
            elif step.step_type == "success" and "generated" in step.description.lower():
                tool_actions.append(step.description[:80])
        return " → ".join(tool_actions) if tool_actions else ""

    # ─── Context Window Management ───

    def _trim_messages(self, messages: list[dict[str, Any]], budget: int = MAX_CONTEXT_TOKENS) -> list[dict[str, Any]]:
        """Trim messages to fit within context budget.

        Strategy: Keep system prompt + last user message intact.
        Trim tool results (replace long content with summary) from oldest first.
        """
        total = _estimate_messages_tokens(messages)
        if total <= budget:
            return messages

        logger.warning("Messages exceed budget: ~%d tokens > %d, trimming...", total, budget)

        # Strategy: compress tool results from oldest to newest
        for i in range(len(messages)):
            if total <= budget:
                break
            msg = messages[i]
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 2000:
                    # Truncate long tool results, keep first/last parts
                    old_tokens = _estimate_tokens(content)
                    # Keep first 500 + last 500 chars + summary
                    try:
                        parsed = json.loads(content)
                        # For file contents, just keep the ok/error status
                        if "file_contents" in parsed:
                            for fname in parsed["file_contents"]:
                                fc = parsed["file_contents"][fname]
                                if len(fc) > 500:
                                    parsed["file_contents"][fname] = fc[:300] + f"\n... ({len(fc)} chars total, truncated) ...\n" + fc[-200:]
                            messages[i]["content"] = json.dumps(parsed)
                        elif "content" in parsed and isinstance(parsed["content"], str) and len(parsed["content"]) > 1000:
                            parsed["content"] = parsed["content"][:500] + f"\n... (truncated {len(parsed['content'])} chars) ...\n" + parsed["content"][-300:]
                            messages[i]["content"] = json.dumps(parsed)
                        elif "current_file_content" in parsed and len(parsed["current_file_content"]) > 1000:
                            fc = parsed["current_file_content"]
                            parsed["current_file_content"] = fc[:500] + f"\n... (truncated {len(fc)} chars) ...\n" + fc[-300:]
                            messages[i]["content"] = json.dumps(parsed)
                    except (json.JSONDecodeError, TypeError):
                        messages[i]["content"] = content[:500] + f"\n... (truncated from {len(content)} chars) ...\n" + content[-300:]
                    new_tokens = _estimate_tokens(messages[i]["content"])
                    total -= (old_tokens - new_tokens)

        # If still over budget, drop middle messages (keep first 3 and last 5)
        if total > budget and len(messages) > 10:
            keep_start = 3  # system + memory + maybe history
            keep_end = 5    # recent context
            removed = messages[keep_start:-keep_end]
            summary = f"[{len(removed)} earlier messages omitted to fit context window]"
            messages = messages[:keep_start] + [{"role": "system", "content": summary}] + messages[-keep_end:]
            logger.warning("Dropped %d middle messages to fit context", len(removed))

        return messages

    TOOL_MAP = {
        "generate_project": "_tool_generate_project",
        "validate_project": "_tool_validate_project",
        "read_file": "_tool_read_file",
        "write_file": "_tool_write_file",
        "list_files": "_tool_list_files",
        "list_assets": "_tool_list_assets",
        "update_memory": "_tool_update_memory",
        "delete_file": "_tool_delete_file",
        "rename_file": "_tool_rename_file",
        "search_files": "_tool_search_files",
        "edit_project_settings": "_tool_edit_project_settings",
        "run_project": "_tool_run_project",
        "create_resource": "_tool_create_resource",
        "patch_file": "_tool_patch_file",
        "export_web": "_tool_export_web",
        "analyze_image": "_tool_analyze_image",
    }

    def _execute_tool(self, tool_name: str, args: dict) -> str:
        """Execute a tool by name and return the result, with auto-injected guides."""
        method_name = self.TOOL_MAP.get(tool_name)
        if not method_name:
            return json.dumps({"ok": False, "error": f"Unknown tool: {tool_name}"})

        method = getattr(self, method_name)
        result = method(args)

        # Auto-inject bound prompt guides into tool result
        guides = self._collect_guides(tool_name, args, result)
        if guides:
            result = self._append_guides_to_result(result, guides)

        return result

    def _collect_guides(self, tool_name: str, args: dict, tool_result: str) -> dict[str, str]:
        """Collect prompt guides to inject for this tool call (skip already-injected)."""
        prompt_names: list[str] = []

        # Static bindings
        prompt_names.extend(TOOL_PROMPTS.get(tool_name, []))

        # Dynamic bindings based on args/result
        prompt_names.extend(_get_dynamic_prompts(tool_name, args, tool_result))

        # Deduplicate and skip already-injected
        guides: dict[str, str] = {}
        for name in prompt_names:
            if name in self._injected_prompts:
                continue
            content = _load_prompt_file(name)
            if content:
                guides[name] = content
                self._injected_prompts.add(name)

        return guides

    def _append_guides_to_result(self, result: str, guides: dict[str, str]) -> str:
        """Append guide content to tool result JSON under __guide__ key."""
        try:
            data = json.loads(result)
            # Build a single guide block with all guides concatenated
            guide_text = "\n\n".join(
                f"--- {name} ---\n{content}" for name, content in guides.items()
            )
            data["__guide__"] = guide_text
            return json.dumps(data)
        except (json.JSONDecodeError, TypeError):
            # If result isn't JSON, append as text
            guide_block = "\n\n__GUIDE__:\n" + "\n\n".join(
                f"--- {name} ---\n{content}" for name, content in guides.items()
            )
            return result + guide_block

    # ─── Agent Loop ───

    def generate_plan(self, user_prompt: str, output_dir: str) -> ProjectPlan:
        """Generate a ProjectPlan from natural language description using agent loop.

        This runs the full agent loop: generate → validate → fix → validate → ...
        Returns the final ProjectPlan after all fixes are applied.
        """
        result = self.agent_generate(user_prompt, output_dir)
        if result.get("plan"):
            return result["plan"]
        raise RuntimeError(result.get("error", "Agent failed to generate project"))

    def agent_generate(
        self,
        user_prompt: str,
        output_dir: str,
        step_callback: Any = None,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run the full agent loop to generate or modify a working project.

        Args:
            user_prompt: Natural language game description or modification request
            output_dir: Where to write the project files
            step_callback: Optional callback(AgentStep) for real-time updates
            images: Optional list of base64 data URI strings for vision input

        Returns:
            dict with 'ok', 'plan', 'steps', 'files', 'project' info
        """
        self._output_dir = output_dir
        self._steps = []
        self._step_callback = step_callback
        self._last_plan = None
        self._files_read_this_loop = set()  # Reset read tracking

        # Check if this is an existing project (continue mode)
        is_existing = self._is_existing_project()
        self._project_generated = is_existing  # If project exists, lock generate_project

        # Load project memory
        memory_content = self._load_project_memory()

        if is_existing:
            self._add_step("thinking", "Continuing work on existing project")
            if memory_content:
                self._add_step("thinking", "Loaded project memory", memory_content[:200])
        else:
            self._add_step("thinking", "Processing your message...", user_prompt)

        # Pre-scan assets and inject into system prompt so LLM ALWAYS knows about them
        asset_inventory = self._scan_assets_for_prompt()

        # Build initial messages — system prompt is lean; guides are auto-injected into tool results
        system_content = SYSTEM_PROMPT
        if asset_inventory:
            system_content += f"\n\n## USER-UPLOADED ASSETS (ALREADY IN PROJECT DIRECTORY):\n\n{asset_inventory}\n\n**YOU MUST USE EVERY SINGLE ASSET LISTED ABOVE.** Do NOT use ColorRect or placeholder graphics."

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
        ]

        # Pre-inject domain-specific guides based on detected asset types
        # This ensures the LLM sees detailed rules BEFORE calling generate_project (which is one-shot)
        if asset_inventory and "### 3D Models" in asset_inventory:
            guide_3d = _load_prompt_file("3d_models")
            if guide_3d:
                messages.append({
                    "role": "system",
                    "content": f"## 3D MODEL GUIDE (READ BEFORE generating any 3D project):\n\n{guide_3d}"
                })
                self._injected_prompts.add("3d_models")

        # Add memory context if available — summarize if too long to prevent attention dilution
        if memory_content:
            prepared_memory = self._prepare_memory_for_injection(memory_content)
            messages.append({
                "role": "system",
                "content": (
                    "## PROJECT MEMORY (background reference only):\n\n"
                    f"{prepared_memory}\n\n"
                    "---\n"
                    "⚠️ This memory is from PREVIOUS sessions. It describes the project's general state.\n"
                    "The user's CURRENT message below is what you must focus on — it may describe NEW issues "
                    "that are NOT mentioned in this memory. Do NOT assume the memory is up-to-date.\n"
                    "After making changes, update memory with `update_memory`."
                )
            })

        # Inject chat history for multi-turn context (project-isolated)
        # Keep budget small to avoid old conversations overshadowing the current request
        chat_history = self._chat_store.get(output_dir)
        if chat_history:
            # Deduplicate: remove history messages that substantially overlap with memory
            if memory_content:
                chat_history = self._deduplicate_history_against_memory(chat_history, memory_content)

            history_budget = 6_000  # ~6K tokens — enough for recent context, not so much it dominates
            history_tokens = 0
            history_to_add = []
            for msg in reversed(chat_history):
                content = msg.get("content", "") if isinstance(msg.get("content"), str) else str(msg.get("content", ""))
                # Strip verbose tool action logs from history — they bloat context with stale details
                if "[Actions:" in content or "[Tool actions:" in content:
                    content = content.split("[Actions:")[0].split("[Tool actions:")[0].strip()
                    if not content:
                        continue  # Skip entries that were only tool action logs
                msg_tokens = _estimate_tokens(content)
                if history_tokens + msg_tokens > history_budget:
                    break
                history_to_add.insert(0, msg)
                history_tokens += msg_tokens

            if history_to_add:
                # Add a separator so LLM knows this is old context
                messages.append({
                    "role": "system",
                    "content": "## PREVIOUS CONVERSATION (for reference — the user's CURRENT request below may be about a completely different topic):"
                })
                for msg in history_to_add:
                    messages.append(msg)
                messages.append({
                    "role": "system",
                    "content": "## END OF PREVIOUS CONVERSATION. Now focus on the user's NEW request below:"
                })

        if is_existing:
            project_name = Path(output_dir).name
            text_content = f"[Project: {project_name}]\n\n[CURRENT REQUEST — this is what you must address NOW]:\n{user_prompt}"
        else:
            text_content = user_prompt

        # Store images in session so the analyze_image tool can access them
        if images:
            self._images = images
            img_hint = (
                f"\n\n[{len(images)} image(s) attached. You CANNOT see them directly. "
                f"Use the `analyze_image` tool with a specific prompt to understand what each image shows. "
                f"Available image indices: 1 to {len(images)}.]"
            )
            text_content += img_hint

        messages.append({"role": "user", "content": text_content})

        # Start with all tools; configure based on mode
        current_tools = list(AGENT_TOOLS)
        if is_existing:
            # Remove generate_project for existing projects
            current_tools = [t for t in current_tools if t["function"]["name"] != "generate_project"]
        if not images:
            # Remove analyze_image when no images attached (avoid confusing the Agent)
            current_tools = [t for t in current_tools if t["function"]["name"] != "analyze_image"]

        iteration = 0
        final_validation_ok = False
        agent_reply = ""  # Natural language reply from agent to show user
        self._post_validation_count = 0  # Track iterations after validation pass
        self._has_validated = False  # Track if validate_project was called at all
        self._has_file_changes = False  # Track if any file modifications were made

        # Dynamic iteration limit based on task
        max_iter = self.MAX_ITERATIONS_EXISTING if is_existing else self.MAX_ITERATIONS_NEW
        # Track if agent is actively working (made tool calls in last iteration)
        last_iter_had_tools = False
        # Track consecutive iterations with no tool calls — for smart early stopping
        consecutive_no_tools = 0

        while iteration < max_iter:
            iteration += 1
            self._add_step("thinking", f"Agent iteration {iteration}/{max_iter}...")
            last_iter_had_tools = False

            try:
                # Trim messages to fit context window before each LLM call
                messages = self._trim_messages(messages)

                # Call LLM with tools
                response = self.llm.invoke_with_tools(
                    messages,
                    tools=current_tools,
                    temperature=0.3,
                    max_tokens=16384,
                )

                if not response:
                    self._add_step("error", "LLM returned empty response")
                    break

                # Extract the response
                message = response.choices[0].message
                finish_reason = response.choices[0].finish_reason

                # Handle content filter - retry with simplified prompt
                if finish_reason == "content_filter" and not message.tool_calls and not message.content:
                    self._add_step("thinking", "Content filter triggered, retrying...")
                    if iteration == 1:
                        messages[0]["content"] = SYSTEM_PROMPT[:2000]
                    continue

                # Add assistant message to history
                msg_dict = message.model_dump(exclude_none=True)
                if msg_dict.get("tool_calls") and not msg_dict.get("content"):
                    msg_dict.pop("content", None)
                messages.append(msg_dict)

                # Capture any text content from the assistant as agent's reply
                if message.content and message.content.strip():
                    agent_reply = message.content.strip()

                # Check if we have tool calls
                if message.tool_calls:
                    last_iter_had_tools = True
                    consecutive_no_tools = 0  # Reset — agent is actively working
                    for tool_call in message.tool_calls:
                        tool_name = tool_call.function.name
                        try:
                            tool_args = json.loads(tool_call.function.arguments)
                        except json.JSONDecodeError:
                            tool_args = {}

                        self._add_step("tool_call", f"Calling tool: {tool_name}", json.dumps(tool_args)[:500])

                        # Execute the tool
                        tool_result = self._execute_tool(tool_name, tool_args)

                        self._add_step("tool_result", f"Tool {tool_name} result", tool_result[:1000])

                        # Add tool result to messages
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_result,
                        })

                        # After project generated: remove generate_project from available tools
                        if tool_name == "generate_project":
                            self._has_file_changes = True
                            try:
                                result_data = json.loads(tool_result)
                                if result_data.get("ok"):
                                    # Remove generate_project so LLM can't call it again
                                    current_tools = [t for t in current_tools if t["function"]["name"] != "generate_project"]
                                    self._add_step("success", "Project generated. Switching to fix-only mode.")
                                    # Check for asset warnings
                                    if result_data.get("ASSET_WARNING"):
                                        self._add_step("error", "Some uploaded assets are not used in the project! Agent will fix this.")
                            except json.JSONDecodeError:
                                pass

                        # Track file modifications
                        if tool_name in ("write_file", "patch_file", "create_resource", "delete_file", "rename_file", "edit_project_settings"):
                            self._has_file_changes = True

                        # Check if validation passed
                        if tool_name == "validate_project":
                            self._has_validated = True
                            try:
                                result_data = json.loads(tool_result)
                                if result_data.get("ok"):
                                    final_validation_ok = True
                                    self._add_step("success", "Project validation passed!")
                                else:
                                    # Validation failed — reset so agent keeps fixing
                                    final_validation_ok = False
                                    self._post_validation_count = 0
                                    error_count = result_data.get("error_count", len(result_data.get("errors", [])))
                                    self._add_step("error", f"Validation found {error_count} error(s). Agent will fix them.")
                            except json.JSONDecodeError:
                                pass

                    # If validation passed, allow a few more iterations for cleanup (update_memory + reply)
                    if final_validation_ok:
                        memory_updated = any(
                            tc.function.name == "update_memory" for tc in message.tool_calls
                        )
                        if memory_updated:
                            # Memory saved, we're done — break immediately
                            break
                        # Give agent up to 3 extra iterations to wrap up (update_memory, reply, etc.)
                        self._post_validation_count += 1
                        if self._post_validation_count >= 3:
                            self._add_step("thinking", "Validation passed, wrapping up.")
                            break
                    else:
                        # Validation failed or hasn't happened — reset counter so agent can keep fixing
                        self._post_validation_count = 0

                elif message.content:
                    # No tool calls, LLM responded with text only
                    consecutive_no_tools += 1
                    last_iter_had_tools = False
                    content_text = message.content.strip()
                    
                    # Check if this looks like a JSON response (not conversational)
                    is_json_like = (
                        content_text.startswith("{")
                        or content_text.startswith("```json")
                        or content_text.startswith("```\n{")
                    )
                    
                    if not is_json_like:
                        # ── Smart early-stop logic ──
                        # Case 1: No file changes at all → pure conversation, return immediately
                        if not self._has_file_changes:
                            self._add_step("thinking", "Agent is responding to the user")
                            result: dict[str, Any] = {
                                "ok": True if self._project_generated else False,
                                "reply": content_text,
                                "conversational": True,
                                "steps": [s.to_dict() for s in self._steps],
                                "files": {},
                                "iterations": iteration,
                                "is_existing": is_existing,
                            }
                            self._chat_store.append(output_dir, {"role": "user", "content": user_prompt})
                            self._chat_store.append(output_dir, {"role": "assistant", "content": content_text})
                            if self._project_generated and self._output_dir:
                                result["files"] = self._collect_project_files()
                                proj_info = self._build_project_info()
                                if proj_info:
                                    result["project"] = proj_info
                            return result

                        # Case 2: Validation already passed → agent is wrapping up, break
                        if final_validation_ok:
                            self._add_step("thinking", "Task complete — validation passed, agent finished.")
                            break

                        # Case 3: Agent gave text but validation hasn't passed yet
                        # Check if agent appears to have only partially completed the task
                        # (made some file changes but stopped with a summary instead of finishing)
                        if self._has_file_changes and consecutive_no_tools == 1:
                            # Agent wrote some files then gave a text summary — nudge it to continue
                            messages.append({
                                "role": "user",
                                "content": (
                                    "You have NOT finished the task yet. You created some files but the full request "
                                    "is not complete. Review the user's original request and your plan — continue "
                                    "creating ALL remaining files and scenes. Do NOT stop until everything is done, "
                                    "then call `validate_project` and `update_memory`."
                                ),
                            })
                            consecutive_no_tools = 0  # Reset so agent gets another chance
                            self._add_step("thinking", "Agent paused mid-task. Nudging to continue...")
                            continue

                        # If consecutive text-only responses >= 2 even after nudge, agent is truly stuck
                        if consecutive_no_tools >= 2:
                            self._add_step("thinking", f"Agent stopped calling tools after {iteration} iterations. Moving to post-loop validation.")
                            break
                        # Otherwise continue — agent might call tools next iteration
                        continue
                    else:
                        # Try JSON extraction as fallback
                        self._add_step("thinking", "LLM responded without tools, trying JSON extraction...")
                        try:
                            raw = _extract_json(content_text)
                            plan = _parse_plan_json(raw, output_dir)
                            from vibe_tools.project_generator import ProjectGenerator
                            gen = ProjectGenerator()
                            gen.generate(plan)
                            self._last_plan = plan
                            self._project_generated = True
                            self._has_file_changes = True
                            self._add_step("success", "Project generated (non-agent fallback). Needs validation.")
                        except Exception as e:
                            # JSON extraction failed, treat as conversational
                            self._add_step("thinking", "Agent is responding to the user")
                            self._chat_store.append(output_dir, {"role": "user", "content": user_prompt})
                            self._chat_store.append(output_dir, {"role": "assistant", "content": content_text})
                            result_conv: dict[str, Any] = {
                                "ok": True,
                                "reply": content_text,
                                "conversational": True,
                                "steps": [s.to_dict() for s in self._steps],
                                "files": {},
                                "iterations": iteration,
                                "is_existing": is_existing,
                            }
                            return result_conv
                    break
                else:
                    consecutive_no_tools += 1
                    last_iter_had_tools = False
                    self._add_step("error", "LLM returned empty message")
                    # If agent has made file changes but returned empty, nudge it to continue
                    if self._has_file_changes and consecutive_no_tools == 1:
                        messages.append({
                            "role": "user",
                            "content": "Continue working. You have not finished the task. Keep creating/modifying files as needed.",
                        })
                        self._add_step("thinking", "Empty response mid-task. Nudging agent to continue...")
                        continue
                    if consecutive_no_tools >= 2:
                        break

            except Exception as e:
                self._add_step("error", f"Agent error: {e}")
                logger.error(f"Agent loop error: {e}", exc_info=True)
                break

            # At iteration limit: extend only if agent is ACTIVELY fixing (called tools this iteration, validation not passed)
            if iteration >= max_iter and last_iter_had_tools and not final_validation_ok and consecutive_no_tools == 0:
                extension = 5
                max_iter += extension
                # Hard cap at 40 to prevent truly infinite loops
                if max_iter > 40:
                    max_iter = 40
                    self._add_step("thinking", f"Hit absolute max iterations ({max_iter}). Moving to post-loop validation.")
                else:
                    self._add_step("thinking", f"Agent still fixing errors — extending by {extension} iterations (new limit: {max_iter})")

        # ── Post-loop: MANDATORY validate → fix → validate cycle ──
        # RULE: The LAST action before returning MUST be a successful validate.
        # If files were changed, we validate. If validation fails, we fix and re-validate.
        # This repeats until validation passes or we exhaust MAX_FIX_ROUNDS.
        MAX_FIX_ROUNDS = 15  # Safety cap for post-loop fix attempts

        if self._output_dir and self._has_file_changes and not final_validation_ok:
            self._add_step("thinking", "Post-loop: files were modified but validation hasn't passed. Starting mandatory validate→fix loop.")

            for fix_round in range(1, MAX_FIX_ROUNDS + 1):
                # ── Step 1: ALWAYS validate first ──
                self._add_step("thinking", f"Post-loop round {fix_round}/{MAX_FIX_ROUNDS}: running validation...")
                try:
                    val_result = self._tool_validate_project({})
                    self._has_validated = True
                    val_data = json.loads(val_result)
                except Exception as e:
                    self._add_step("error", f"Post-loop validation error: {e}")
                    logger.error(f"Post-loop validation error: {e}", exc_info=True)
                    break

                if val_data.get("ok"):
                    final_validation_ok = True
                    self._add_step("success", f"Validation passed on round {fix_round}!")
                    break

                # ── Step 2: Validation failed — send errors to LLM for fixing ──
                error_count = val_data.get("error_count", len(val_data.get("errors", [])))
                self._add_step("error", f"Validation found {error_count} error(s). Asking agent to fix...")

                fix_messages = list(messages) if messages else [{"role": "system", "content": SYSTEM_PROMPT}]
                fix_messages.append({
                    "role": "user",
                    "content": (
                        f"MANDATORY FIX: Validation found {error_count} error(s). You MUST fix ALL of them.\n\n"
                        f"Validation results:\n{val_result}\n\n"
                        f"Instructions:\n"
                        f"1. Use read_file to read each broken file\n"
                        f"2. Use write_file or patch_file to fix the errors\n"
                        f"3. Do NOT call validate_project — I will validate after you finish fixing\n"
                        f"4. Fix EVERY error listed above, not just some of them"
                    ),
                })

                # Give LLM up to 8 iterations per fix round to apply fixes (NO validate here — we do it ourselves)
                fix_iter = 0
                made_changes = False
                fix_tools = [t for t in AGENT_TOOLS if t["function"]["name"] not in ("generate_project", "validate_project")]

                while fix_iter < 8:
                    fix_iter += 1
                    try:
                        fix_messages = self._trim_messages(fix_messages)
                        response = self.llm.invoke_with_tools(
                            fix_messages, tools=fix_tools, temperature=0.3, max_tokens=16384,
                        )
                    except Exception as e:
                        self._add_step("error", f"Post-loop LLM error: {e}")
                        logger.error(f"Post-loop LLM error: {e}", exc_info=True)
                        break

                    if not response:
                        break

                    message = response.choices[0].message
                    msg_dict = message.model_dump(exclude_none=True)
                    if msg_dict.get("tool_calls") and not msg_dict.get("content"):
                        msg_dict.pop("content", None)
                    fix_messages.append(msg_dict)

                    # Don't overwrite agent_reply with fix-loop intermediate messages
                    # (the final summary reply will be generated post-loop if needed)

                    if message.tool_calls:
                        for tool_call in message.tool_calls:
                            tool_name = tool_call.function.name
                            try:
                                tool_args = json.loads(tool_call.function.arguments)
                            except json.JSONDecodeError:
                                tool_args = {}

                            self._add_step("tool_call", f"[Fix] Calling: {tool_name}", json.dumps(tool_args)[:500])
                            tool_result_str = self._execute_tool(tool_name, tool_args)
                            self._add_step("tool_result", f"[Fix] {tool_name} result", tool_result_str[:1000])

                            fix_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": tool_result_str,
                            })

                            if tool_name in ("write_file", "patch_file", "create_resource", "delete_file", "rename_file", "edit_project_settings"):
                                made_changes = True
                    else:
                        # LLM responded with text only — done fixing for this round
                        break

                if not made_changes:
                    # LLM didn't make any changes — it's stuck, no point continuing
                    self._add_step("error", "Agent made no file changes during fix round. Cannot fix further.")
                    break
                # Loop back to Step 1: validate again with the fixes applied

            # ── Final guarantee: if we exited the loop, do one last validate to confirm status ──
            if not final_validation_ok and self._output_dir:
                self._add_step("thinking", "Final validation check...")
                try:
                    final_val = self._tool_validate_project({})
                    self._has_validated = True
                    final_val_data = json.loads(final_val)
                    if final_val_data.get("ok"):
                        final_validation_ok = True
                        self._add_step("success", "Final validation passed!")
                    else:
                        ec = final_val_data.get("error_count", len(final_val_data.get("errors", [])))
                        self._add_step("error", f"Final validation still has {ec} error(s). Returning with errors.")
                except Exception as e:
                    self._add_step("error", f"Final validation error: {e}")
                    logger.error(f"Final validation error: {e}", exc_info=True)

        # ── Ensure agent provides a meaningful reply to the user ──
        # If file changes were made but agent_reply is empty/generic, force a summary reply
        if self._has_file_changes and is_existing:
            needs_reply = (
                not agent_reply
                or len(agent_reply) < 30
                or agent_reply.lower().startswith(("all running", "all done", "all good", "everything"))
            )
            if needs_reply:
                self._add_step("thinking", "Generating summary reply for the user...")
                try:
                    tool_summary = self._build_tool_summary()
                    summary_messages = [
                        {"role": "system", "content": (
                            "You are GodotVibe. The user asked you to fix bugs or modify their Godot project. "
                            "You have already made the changes using tools. Now write a BRIEF reply to the user "
                            "explaining what you changed and why. Address each issue the user mentioned. "
                            "Respond in the SAME LANGUAGE as the user. Be specific — mention file names, "
                            "what the bug was, and how you fixed it. Do NOT just say 'all fixed'."
                        )},
                        {"role": "user", "content": (
                            f"User's original request:\n{user_prompt}\n\n"
                            f"Actions I took:\n{tool_summary or 'Various file modifications'}\n\n"
                            f"Validation result: {'PASSED' if final_validation_ok else 'FAILED'}\n\n"
                            f"Write a reply to the user summarizing what was done."
                        )},
                    ]
                    summary_response = self.llm.invoke(summary_messages, temperature=0.3, max_tokens=2048)
                    if summary_response and summary_response.strip():
                        agent_reply = summary_response.strip()
                except Exception as e:
                    logger.error(f"Failed to generate summary reply: {e}")
                    # Fall back to a basic summary
                    if not agent_reply:
                        tool_summary = self._build_tool_summary()
                        agent_reply = f"Changes applied. {tool_summary}" if tool_summary else "Changes applied."

        # Collect final files
        files = self._collect_project_files()

        # Save to project-isolated chat history
        # Only store the conversational reply — NOT tool action logs (they pollute future context)
        self._chat_store.append(output_dir, {"role": "user", "content": user_prompt})
        if agent_reply:
            self._chat_store.append(output_dir, {"role": "assistant", "content": agent_reply})
        else:
            # If no reply was generated, store a brief summary so history isn't empty
            tool_summary = self._build_tool_summary()
            if tool_summary:
                self._chat_store.append(output_dir, {"role": "assistant", "content": f"[Made changes: {tool_summary[:200]}]"})

        # Build result
        final_result: dict[str, Any] = {
            "ok": final_validation_ok,
            "steps": [s.to_dict() for s in self._steps],
            "files": files,
            "iterations": iteration,
            "is_existing": is_existing,
            "reply": agent_reply,  # Natural language summary from agent
        }

        proj_info = self._build_project_info()
        if proj_info:
            final_result["project"] = proj_info
            if self._last_plan:
                final_result["plan"] = self._last_plan
        elif is_existing:
            # For existing projects, consider it OK if no fatal errors in steps
            if not any(s.step_type == "error" for s in self._steps):
                final_result["ok"] = True

        return final_result

    def chat(self, user_message: str, output_dir: str | None = None) -> dict[str, Any]:
        """Chat with the LLM agent. If output_dir provided, will generate/modify project."""
        if output_dir:
            result = self.agent_generate(user_message, output_dir)
            reply_parts = []

            if result.get("ok"):
                proj = result.get("project", {})
                if proj:
                    reply_parts.append(f"Project **{proj.get('name', 'Game')}** {'updated' if result.get('is_existing') else 'generated'} successfully!")
                    reply_parts.append(f"\nType: `{proj.get('game_type', 'blank')}`")
                    reply_parts.append(f"Output: `{proj.get('output_dir', '')}`")
                    reply_parts.append(f"Scenes: {', '.join(proj.get('scenes', []))}")
                    reply_parts.append(f"Scripts: {', '.join(proj.get('scripts', []))}")
                else:
                    reply_parts.append("Changes applied successfully!")
                reply_parts.append(f"\nAgent completed in {result.get('iterations', 0)} iteration(s).")
            else:
                reply_parts.append("Project work completed with some issues.")
                error_steps = [s for s in result.get("steps", []) if s["type"] == "error"]
                if error_steps:
                    reply_parts.append("\nErrors encountered:")
                    for s in error_steps:
                        reply_parts.append(f"- {s['description']}")

            return {
                "reply": "\n".join(reply_parts),
                "plan": result.get("plan"),
                "steps": result.get("steps", []),
                "files": result.get("files", {}),
                "is_existing": result.get("is_existing", False),
            }
        else:
            # Simple chat without project generation
            self._chat_store.append(None, {"role": "user", "content": user_message})
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            messages.extend(self._chat_store.get(None))

            response = self.llm.invoke(messages, temperature=0.3, max_tokens=4096)
            if not response:
                return {"reply": "Sorry, I couldn't generate a response.", "plan": None}

            self._chat_store.append(None, {"role": "assistant", "content": response})
            return {"reply": response, "plan": None}

    def reset_chat(self):
        """Clear chat history for the current project."""
        if self._output_dir:
            self._chat_store.clear(self._output_dir)


# ─── Game Generator (shared resources + session factory) ───

class GameGenerator:
    """LLM-powered game project generator — thread-safe session factory.

    Holds SHARED resources (LLM client, chat history store) and creates
    per-request AgentSession instances for concurrent safety.

    Multiple users can call agent_generate()/chat() simultaneously —
    each call gets its own AgentSession with isolated mutable state.
    """

    # Dynamic iteration limits (class-level constants, used by AgentSession)
    MAX_ITERATIONS_NEW = AgentSession.MAX_ITERATIONS_NEW
    MAX_ITERATIONS_EXISTING = AgentSession.MAX_ITERATIONS_EXISTING
    MAX_ITERATIONS_CHAT = AgentSession.MAX_ITERATIONS_CHAT

    # The agent model for code generation. Set via AGENT_MODEL env var.

    AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-4-20250514")

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.llm = SimpleLLMProvider(
            model=self.AGENT_MODEL,
            api_key=api_key,
            base_url=base_url,
        )
        self._chat_store = ChatHistoryStore()

    def _create_session(self) -> AgentSession:
        """Create a new isolated AgentSession for a single request."""
        return AgentSession(llm=self.llm, chat_store=self._chat_store)

    def agent_generate(
        self,
        user_prompt: str,
        output_dir: str,
        step_callback: Any = None,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run the full agent loop (thread-safe — creates isolated session)."""
        session = self._create_session()
        return session.agent_generate(user_prompt, output_dir, step_callback, images)

    def generate_plan(self, user_prompt: str, output_dir: str) -> ProjectPlan:
        """Generate a ProjectPlan from natural language (thread-safe)."""
        session = self._create_session()
        return session.generate_plan(user_prompt, output_dir)

    def chat(self, user_message: str, output_dir: str | None = None) -> dict[str, Any]:
        """Chat with the LLM agent (thread-safe)."""
        session = self._create_session()
        return session.chat(user_message, output_dir)

    def reset_chat(self, project_dir: str | None = None):
        """Clear chat history for a specific project."""
        self._chat_store.clear(project_dir)

    def clear_all_chat(self):
        """Clear all chat histories."""
        self._chat_store.clear_all()
