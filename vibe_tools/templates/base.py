"""Base class for game templates."""

from __future__ import annotations

from abc import ABC, abstractmethod

from vibe_tools.models import (
	ProjectPlan, ProjectSettings, SceneDesc, ScriptDesc, NodeDesc, InputEvent,
)


class GameTemplate(ABC):
	"""Base class for all game templates."""

	@property
	@abstractmethod
	def name(self) -> str:
		"""Template identifier."""
		...

	@property
	@abstractmethod
	def description(self) -> str:
		"""Human-readable description."""
		...

	@abstractmethod
	def get_scenes(self) -> list[SceneDesc]:
		"""Return all scene descriptions for this template."""
		...

	@abstractmethod
	def get_scripts(self) -> list[ScriptDesc]:
		"""Return all script descriptions for this template."""
		...

	def get_settings(self) -> ProjectSettings:
		"""Return project settings. Override for custom settings."""
		return ProjectSettings()

	def create_plan(self, project_name: str, output_dir: str) -> ProjectPlan:
		"""Create a complete ProjectPlan from this template."""
		from vibe_tools.models import GameType
		settings = self.get_settings()
		settings.name = project_name

		return ProjectPlan(
			name=project_name,
			game_type=GameType(self.name),
			output_dir=output_dir,
			settings=settings,
			scenes=self.get_scenes(),
			scripts=self.get_scripts(),
		)
