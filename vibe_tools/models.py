"""Core data models for GodotVibe - the data contract between all modules.

Provides centralized constants, type-safe dataclasses with validation,
and serialization utilities used throughout the project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Any


# ─── Centralized Asset / File Constants ───

ASSET_EXTENSIONS_IMAGE = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tga", ".hdr", ".exr", ".svg",
})

ASSET_EXTENSIONS_AUDIO = frozenset({
    ".wav", ".ogg", ".mp3",
})

ASSET_EXTENSIONS_MODEL = frozenset({
    ".glb", ".gltf", ".obj", ".fbx", ".dae",
})

ASSET_EXTENSIONS_FONT = frozenset({
    ".ttf", ".otf", ".woff", ".woff2",
})

ASSET_EXTENSIONS_ALL = (
    ASSET_EXTENSIONS_IMAGE | ASSET_EXTENSIONS_AUDIO
    | ASSET_EXTENSIONS_MODEL | ASSET_EXTENSIONS_FONT
)

# Extensions Godot considers as project source files
PROJECT_SOURCE_EXTENSIONS = frozenset({
    ".tscn", ".gd", ".godot", ".tres", ".gdshader", ".svg",
})

# Directories to ignore when scanning a Godot project
IGNORED_DIRS = frozenset({".godot", ".import", "__pycache__"})


def classify_asset(ext: str) -> str:
    """Return the asset category for a file extension (lowercase, with dot)."""
    ext = ext.lower()
    if ext in ASSET_EXTENSIONS_IMAGE:
        return "image"
    if ext in ASSET_EXTENSIONS_AUDIO:
        return "audio"
    if ext in ASSET_EXTENSIONS_MODEL:
        return "model"
    if ext in ASSET_EXTENSIONS_FONT:
        return "font"
    return "other"


def is_ignored_path(rel_path: str) -> bool:
    """Check whether a relative project path should be skipped (e.g. .godot/)."""
    parts = PurePosixPath(rel_path).parts
    return any(p in IGNORED_DIRS for p in parts)


# ─── Enums ───

class GameType(str, Enum):
    """Supported game type identifiers."""
    BLANK = "blank"
    PLATFORMER_2D = "platformer_2d"
    TOPDOWN_2D = "topdown_2d"
    FPS_3D = "fps_3d"
    TPS_3D = "tps_3d"


# ─── Scene / Script Descriptors ───

@dataclass
class NodeDesc:
    """Description of a single node in the scene tree."""
    name: str
    type: str  # Godot class name, e.g. "CharacterBody2D"
    properties: dict[str, Any] = field(default_factory=dict)
    script: str | None = None  # res:// path to attached script
    instance: str | None = None  # res:// path to a PackedScene (.glb/.tscn) to instance
    groups: list[str] = field(default_factory=list)
    children: list[NodeDesc] = field(default_factory=list)


@dataclass
class ExtResource:
    """An external resource reference in a .tscn file."""
    res_type: str  # e.g. "Script", "Texture2D", "PackedScene"
    path: str  # res:// path
    uid: str | None = None  # optional UID


@dataclass
class SubResource:
    """An inline sub-resource in a .tscn file."""
    res_type: str  # e.g. "RectangleShape2D", "CircleShape2D"
    res_id: str  # unique ID within the scene
    properties: dict[str, str] = field(default_factory=dict)


@dataclass
class SceneDesc:
    """Description of a complete .tscn scene file."""
    filename: str  # e.g. "main.tscn", "player.tscn"
    root: NodeDesc  # root node of the scene tree
    ext_resources: list[ExtResource] = field(default_factory=list)
    sub_resources: list[SubResource] = field(default_factory=list)


@dataclass
class ExportVar:
    """An @export variable in GDScript."""
    name: str
    type: str  # e.g. "float", "int", "Vector2"
    default: str | None = None  # default value as string
    hint: str | None = None  # e.g. "@export_range(0, 100)"


@dataclass
class OnReadyVar:
    """An @onready variable in GDScript."""
    name: str
    node_path: str  # e.g. "$Sprite2D"
    type: str | None = None  # optional type hint


@dataclass
class FunctionDesc:
    """A function in GDScript."""
    name: str  # e.g. "_ready", "_physics_process"
    params: list[str] = field(default_factory=list)  # e.g. ["delta: float"]
    body: str = "pass"  # function body (tab-indented lines)
    is_override: bool = False  # if it overrides a virtual method


@dataclass
class ScriptDesc:
    """Description of a .gd GDScript file."""
    filename: str  # e.g. "player.gd"
    extends: str  # e.g. "CharacterBody2D"
    class_name: str | None = None
    exports: list[ExportVar] = field(default_factory=list)
    onready_vars: list[OnReadyVar] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    functions: list[FunctionDesc] = field(default_factory=list)
    raw_code: str | None = None  # if set, use this raw code instead of generating


# ─── Project Settings ───

@dataclass
class InputEvent:
    """An input event for action mapping."""
    type: str  # "key", "mouse_button", "joypad_button", "joypad_motion"
    value: str  # key code, button index, etc.


@dataclass
class ProjectSettings:
    """project.godot settings."""
    name: str = "VibeCodingGame"
    main_scene: str = "res://main.tscn"
    window_width: int = 1152
    window_height: int = 648
    stretch_mode: str = "canvas_items"
    stretch_aspect: str = "expand"
    custom: dict[str, dict[str, Any]] = field(default_factory=dict)
    input_actions: dict[str, list[InputEvent]] = field(default_factory=dict)


@dataclass
class ProjectPlan:
    """Complete project generation plan - the central data structure."""
    name: str
    game_type: GameType
    output_dir: str  # absolute path to output directory
    settings: ProjectSettings = field(default_factory=ProjectSettings)
    scenes: list[SceneDesc] = field(default_factory=list)
    scripts: list[ScriptDesc] = field(default_factory=list)

    def get_main_scene(self) -> SceneDesc | None:
        """Get the main scene description."""
        main = self.settings.main_scene.replace("res://", "")
        for scene in self.scenes:
            if scene.filename == main:
                return scene
        return None


# ─── API Knowledge Base Models ───

@dataclass
class APIParam:
    """A parameter of an API method."""
    name: str
    type: str
    default: str | None = None


@dataclass
class APIMethod:
    """A method in a Godot API class."""
    name: str
    return_type: str = "void"
    description: str = ""
    params: list[APIParam] = field(default_factory=list)
    qualifiers: str = ""  # e.g. "const", "virtual"


@dataclass
class APIProperty:
    """A property of a Godot API class."""
    name: str
    type: str
    default: str | None = None
    description: str = ""
    setter: str = ""
    getter: str = ""


@dataclass
class APISignal:
    """A signal of a Godot API class."""
    name: str
    description: str = ""
    params: list[APIParam] = field(default_factory=list)


@dataclass
class APIConstant:
    """A constant in a Godot API class."""
    name: str
    value: str
    description: str = ""
    enum_name: str | None = None


@dataclass
class APIEnum:
    """An enum in a Godot API class."""
    name: str
    values: list[APIConstant] = field(default_factory=list)


@dataclass
class APIClass:
    """Parsed Godot API class from XML documentation."""
    name: str
    inherits: str | None = None
    brief_description: str = ""
    description: str = ""
    category: str = ""  # assigned category: "2d", "3d", "gui", "audio", etc.
    methods: list[APIMethod] = field(default_factory=list)
    properties: list[APIProperty] = field(default_factory=list)
    signals: list[APISignal] = field(default_factory=list)
    constants: list[APIConstant] = field(default_factory=list)
    enums: list[APIEnum] = field(default_factory=list)
