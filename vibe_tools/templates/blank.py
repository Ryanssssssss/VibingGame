"""Blank project template - minimal starting point."""

from __future__ import annotations

from vibe_tools.models import SceneDesc, ScriptDesc, NodeDesc, ProjectSettings
from vibe_tools.templates.base import GameTemplate


class BlankTemplate(GameTemplate):
	@property
	def name(self) -> str:
		return "blank"

	@property
	def description(self) -> str:
		return "Empty project with a root Node - a clean starting point"

	def get_scenes(self) -> list[SceneDesc]:
		return [
			SceneDesc(
				filename="main.tscn",
				root=NodeDesc(name="Main", type="Node"),
			),
		]

	def get_scripts(self) -> list[ScriptDesc]:
		return []

	def get_settings(self) -> ProjectSettings:
		return ProjectSettings(
			main_scene="res://main.tscn",
			window_width=1152,
			window_height=648,
		)
