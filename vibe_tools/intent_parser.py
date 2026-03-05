"""Intent parser - converts user input into a ProjectPlan.

Current implementation uses structured interactive prompts.
Designed with an abstract base to allow future LLM integration.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

from vibe_tools.models import ProjectPlan, GameType
from vibe_tools.templates import TEMPLATES, get_template


class IntentProvider(ABC):
	"""Abstract base for intent parsing. Subclass for LLM integration."""

	@abstractmethod
	def parse(self) -> ProjectPlan | None:
		"""Parse user intent and return a ProjectPlan, or None if cancelled."""
		...


class LLMIntentProvider(IntentProvider):
	"""Placeholder for future LLM-based intent parsing."""

	def __init__(self, api_key: str | None = None, model: str = "gpt-4"):
		self.api_key = api_key
		self.model = model

	def parse(self) -> ProjectPlan | None:
		raise NotImplementedError(
			"LLM intent parsing not yet implemented. "
			"Set up an API key and implement natural language → ProjectPlan conversion."
		)


class InteractiveIntentParser(IntentProvider):
	"""Interactive CLI-based intent parser with structured prompts."""

	def __init__(self, default_output_dir: str | None = None):
		self._default_output = default_output_dir or str(
			Path.home() / "GodotVibeProjects"
		)

	def parse(self) -> ProjectPlan | None:
		"""Walk user through project creation with interactive prompts."""
		print("\n  🎮 Create a New Game Project")
		print("  ═══════════════════════════════════════")

		# Step 1: Project name
		name = input("\n  Project name [MyGame]: ").strip()
		if not name:
			name = "MyGame"

		# Sanitize name for directory
		dir_name = name.replace(" ", "_")

		# Step 2: Game type selection
		print("\n  Choose a game template:")
		template_list = list(TEMPLATES.items())
		for i, (key, tmpl_cls) in enumerate(template_list, 1):
			tmpl = tmpl_cls()
			print(f"    {i}. [{key}] {tmpl.description}")

		choice = input(f"\n  Select template [1-{len(template_list)}]: ").strip()
		try:
			idx = int(choice) - 1
			if idx < 0 or idx >= len(template_list):
				print("  ❌ Invalid selection.")
				return None
		except ValueError:
			# Try matching by name
			matching = [k for k in TEMPLATES if choice.lower() in k.lower()]
			if matching:
				template_key = matching[0]
			else:
				print("  ❌ Invalid selection.")
				return None
		else:
			template_key = template_list[idx][0]

		# Step 3: Output directory
		default_path = os.path.join(self._default_output, dir_name)
		output = input(f"\n  Output directory [{default_path}]: ").strip()
		if not output:
			output = default_path

		# Step 4: Window size
		print("\n  Window size:")
		print("    1. 1152x648 (default)")
		print("    2. 1920x1080 (Full HD)")
		print("    3. 1280x720 (HD)")
		print("    4. 800x600 (Classic)")
		win_choice = input("  Select [1]: ").strip()

		window_sizes = {
			"1": (1152, 648),
			"2": (1920, 1080),
			"3": (1280, 720),
			"4": (800, 600),
		}
		width, height = window_sizes.get(win_choice, (1152, 648))

		# Create the plan from template
		template = get_template(template_key)
		plan = template.create_plan(name, output)
		plan.settings.window_width = width
		plan.settings.window_height = height

		return plan


class QuickIntentParser(IntentProvider):
	"""Quick parser for non-interactive use - creates from template name directly."""

	def __init__(self, template_name: str, project_name: str, output_dir: str):
		self.template_name = template_name
		self.project_name = project_name
		self.output_dir = output_dir

	def parse(self) -> ProjectPlan | None:
		try:
			template = get_template(self.template_name)
		except ValueError:
			return None
		return template.create_plan(self.project_name, self.output_dir)
