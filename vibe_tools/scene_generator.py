"""Godot .tscn scene file generator.

Generates valid Godot 4.x text scene files (.tscn) with format version 3.
"""

from __future__ import annotations

import hashlib
import random
import string
from typing import Sequence

from vibe_tools.models import NodeDesc, SceneDesc, ExtResource, SubResource


def _gen_uid() -> str:
    """Generate a uid:// style UID (13 alphanumeric chars)."""
    chars = string.ascii_lowercase + string.digits
    uid = "".join(random.choices(chars, k=13))
    return f"uid://{uid}"


def _gen_res_id(prefix: str = "") -> str:
    """Generate a resource string ID like '1_abc5x'."""
    chars = string.ascii_lowercase + string.digits
    suffix = "".join(random.choices(chars, k=5))
    return f"{prefix}_{suffix}" if prefix else suffix


class SceneWriter:
    """Writes a .tscn file from a SceneDesc.

    Tracks external / sub resources, collects the node tree,
    and serialises everything into Godot 4.x format=3.
    """

    def __init__(self) -> None:
        self._ext_resources: list[ExtResource] = []
        self._sub_resources: list[SubResource] = []
        self._ext_id_counter: int = 0
        self._node_lines: list[str] = []
        self._connection_lines: list[str] = []
        # Map ext_resource path → assigned ID for dedup
        self._ext_path_to_id: dict[str, str] = {}

    # ─── Public helpers ───

    def add_ext_resource(self, res_type: str, path: str, uid: str | None = None) -> str:
        """Add an external resource and return its ID string.

        Deduplicates by *path* — if the same path was already registered
        the existing ID is returned without creating a new entry.
        """
        existing_id = self._ext_path_to_id.get(path)
        if existing_id is not None:
            return existing_id
        self._ext_id_counter += 1
        res_id = f"{self._ext_id_counter}_{_gen_res_id()}"
        er = ExtResource(res_type=res_type, path=path, uid=res_id)
        self._ext_resources.append(er)
        self._ext_path_to_id[path] = res_id
        return res_id

    def add_sub_resource(self, res_type: str, properties: dict[str, str] | None = None) -> str:
        """Add a sub-resource and return its ID string."""
        res_id = f"{res_type}_{_gen_res_id()}"
        sr = SubResource(
            res_type=res_type,
            res_id=res_id,
            properties=properties or {},
        )
        self._sub_resources.append(sr)
        return res_id

    def add_connection(self, signal_name: str, from_path: str, to_path: str, method: str) -> None:
        """Add a signal connection."""
        self._connection_lines.append(
            f'[connection signal="{signal_name}" from="{from_path}" to="{to_path}" method="{method}"]'
        )

    # ─── Generation entry point ───

    def generate(self, scene: SceneDesc) -> str:
        """Generate the complete .tscn file content."""
        # Initialise from scene's pre-existing resources
        self._ext_resources = list(scene.ext_resources)
        self._sub_resources = list(scene.sub_resources)
        self._ext_id_counter = len(self._ext_resources)
        self._ext_path_to_id = {er.path: er.uid for er in self._ext_resources if er.uid}
        self._node_lines = []
        self._connection_lines = []

        # Walk the scene tree
        self._collect_nodes(scene.root, is_root=True, parent_path="")

        return self._build_output()

    # ─── Internals ───

    def _collect_nodes(self, node: NodeDesc, *, is_root: bool, parent_path: str) -> None:
        """Recursively collect node definitions."""
        # Header
        parts: list[str] = [f'[node name="{node.name}" type="{node.type}"']
        if not is_root:
            parts.append(f' parent="{parent_path}"')
        if node.groups:
            groups_str = ", ".join(f'"{g}"' for g in node.groups)
            parts.append(f" groups=[{groups_str}]")
        header = "".join(parts) + "]"
        self._node_lines.append(header)

        # Script attachment
        if node.script:
            script_id = self.add_ext_resource("Script", node.script)
            self._node_lines.append(f'script = ExtResource("{script_id}")')

        # Properties
        for key, value in node.properties.items():
            self._node_lines.append(f"{key} = {value}")

        self._node_lines.append("")  # blank separator

        # Children — compute parent_path correctly for arbitrary nesting depth
        for child in node.children:
            if is_root:
                child_parent = "."
            else:
                child_parent = f"{parent_path}/{node.name}" if parent_path != "." else node.name
            self._collect_nodes(child, is_root=False, parent_path=child_parent)

    def _build_output(self) -> str:
        """Build the final .tscn file content."""
        lines: list[str] = []

        # Header
        uid = _gen_uid()
        lines.append(f'[gd_scene format=3 uid="{uid}"]')
        lines.append("")

        # External resources
        for er in self._ext_resources:
            line = f'[ext_resource type="{er.res_type}" path="{er.path}" id="{er.uid}"]'
            lines.append(line)
        if self._ext_resources:
            lines.append("")

        # Sub-resources
        for sr in self._sub_resources:
            lines.append(f'[sub_resource type="{sr.res_type}" id="{sr.res_id}"]')
            for key, value in sr.properties.items():
                lines.append(f"{key} = {value}")
            lines.append("")

        # Nodes
        lines.extend(self._node_lines)

        # Connections
        if self._connection_lines:
            lines.append("")
            lines.extend(self._connection_lines)

        # Ensure trailing newline
        content = "\n".join(lines)
        if not content.endswith("\n"):
            content += "\n"
        return content


def generate_scene(scene: SceneDesc) -> str:
    """Generate a .tscn file from a SceneDesc. Returns the file content."""
    writer = SceneWriter()
    return writer.generate(scene)
