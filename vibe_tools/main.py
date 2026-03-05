#!/usr/bin/env python3
"""GodotVibe - Vibe Coding for Godot Engine game development.

Usage:
    python -m vibe_tools              # Start web UI (default)
    python -m vibe_tools web          # Start web UI
    python -m vibe_tools cli          # Interactive CLI mode
    python -m vibe_tools new          # Create a new game project (CLI)
    python -m vibe_tools api          # Build API knowledge base
    python -m vibe_tools templates    # List templates
"""

from __future__ import annotations

import os
import sys
import argparse
from pathlib import Path

# Ensure UTF-8 output on Windows
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
	sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
	sys.stderr.reconfigure(encoding="utf-8")

# Add parent directory to path so we can import vibe_tools
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vibe_tools.models import GameType, ProjectPlan, ProjectSettings
from vibe_tools.intent_parser import InteractiveIntentParser
from vibe_tools.project_generator import ProjectGenerator
from vibe_tools.godot_runner import GodotRunner
from vibe_tools.api_parser import APIKnowledgeBase

# Engine root directory
ENGINE_ROOT = Path(__file__).resolve().parent.parent
GODOT_EXE = ENGINE_ROOT / "bin" / "godot.windows.editor.x86_64.exe"
DOC_CLASSES_DIR = ENGINE_ROOT / "doc" / "classes"
API_CACHE_DIR = Path(__file__).resolve().parent / "api_cache"


def print_banner():
	"""Print the GodotVibe banner."""
	banner = r"""
 ╔══════════════════════════════════════════════════╗
 ║          🎮 GodotVibe - Vibe Coding 🎮          ║
 ║     Turn your ideas into Godot games instantly   ║
 ╚══════════════════════════════════════════════════╝
    """
	print(banner)


def print_help():
	"""Print available commands."""
	print("\n  Available commands:")
	print("  ─────────────────────────────────────────")
	print("  new       Create a new game project")
	print("  modify    Modify an existing project")
	print("  preview   Open project in Godot editor")
	print("  api       Query Godot API knowledge base")
	print("  templates List available game templates")
	print("  help      Show this help message")
	print("  quit      Exit GodotVibe")
	print()


def cmd_new(runner: GodotRunner, api_kb: APIKnowledgeBase | None):
	"""Create a new game project interactively."""
	parser = InteractiveIntentParser()
	plan = parser.parse()
	if plan is None:
		print("\n  ❌ Project creation cancelled.")
		return

	print(f"\n  📋 Project Plan:")
	print(f"     Name: {plan.name}")
	print(f"     Type: {plan.game_type.value}")
	print(f"     Output: {plan.output_dir}")
	print(f"     Scenes: {len(plan.scenes)}")
	print(f"     Scripts: {len(plan.scripts)}")

	confirm = input("\n  Generate project? [Y/n] ").strip().lower()
	if confirm and confirm != "y":
		print("  ❌ Cancelled.")
		return

	generator = ProjectGenerator()
	output_path = generator.generate(plan)
	print(f"\n  ✅ Project generated at: {output_path}")

	preview = input("  Open in Godot editor? [Y/n] ").strip().lower()
	if not preview or preview == "y":
		runner.open_editor(output_path)


def cmd_preview(runner: GodotRunner):
	"""Open a project in Godot editor."""
	project_path = input("  Enter project path: ").strip()
	if not project_path:
		print("  ❌ No path provided.")
		return
	path = Path(project_path)
	if not path.exists():
		print(f"  ❌ Path not found: {path}")
		return
	runner.open_editor(str(path))


def cmd_templates():
	"""List available game templates."""
	from vibe_tools.templates import TEMPLATES
	print("\n  Available game templates:")
	print("  ─────────────────────────────────────────")
	for key, tmpl_cls in TEMPLATES.items():
		tmpl = tmpl_cls()
		print(f"  {key:20s} {tmpl.description}")
	print()


def cmd_api(api_kb: APIKnowledgeBase | None):
	"""Query the API knowledge base."""
	if api_kb is None:
		print("  ⚠️  API knowledge base not loaded. Building...")
		return

	query = input("  Search API (class/method/property): ").strip()
	if not query:
		print(f"  📊 Loaded {len(api_kb.classes)} classes")
		print(f"     Categories: {', '.join(sorted(api_kb.categories.keys()))}")
		return

	results = api_kb.search(query)
	if not results:
		print(f"  No results for '{query}'")
		return

	for cls in results[:10]:
		print(f"  📦 {cls.name}", end="")
		if cls.inherits:
			print(f" extends {cls.inherits}", end="")
		print(f" [{cls.category}]")
		if cls.brief_description:
			print(f"     {cls.brief_description[:80]}")
	if len(results) > 10:
		print(f"  ... and {len(results) - 10} more results")


def cmd_web(host: str = "127.0.0.1", port: int = 8899):
	"""Start the web UI server."""
	from vibe_tools.web_server import start_server
	start_server(host=host, port=port)


def interactive_mode():
	"""Run the interactive CLI loop."""
	print_banner()

	# Initialize components
	runner = GodotRunner(str(GODOT_EXE))

	# Try to load API knowledge base
	api_kb = None
	if API_CACHE_DIR.exists() and (API_CACHE_DIR / "api_knowledge.json").exists():
		print("  📚 Loading API knowledge base from cache...")
		api_kb = APIKnowledgeBase.load_from_cache(str(API_CACHE_DIR / "api_knowledge.json"))
		print(f"  ✅ Loaded {len(api_kb.classes)} classes")
	else:
		print("  💡 Tip: Run 'api' command to build the API knowledge base")

	print_help()

	while True:
		try:
			cmd = input("  godot-vibe> ").strip().lower()
		except (EOFError, KeyboardInterrupt):
			print("\n  Goodbye! 👋")
			break

		if not cmd:
			continue
		elif cmd in ("quit", "exit", "q"):
			print("  Goodbye! 👋")
			break
		elif cmd == "new":
			cmd_new(runner, api_kb)
		elif cmd == "preview":
			cmd_preview(runner)
		elif cmd == "templates":
			cmd_templates()
		elif cmd == "api":
			if api_kb is None:
				print("  📚 Building API knowledge base (first time takes ~3 seconds)...")
				api_kb = APIKnowledgeBase.build_from_xml(str(DOC_CLASSES_DIR))
				api_kb.save_to_cache(str(API_CACHE_DIR / "api_knowledge.json"))
				print(f"  ✅ Built and cached {len(api_kb.classes)} classes")
			else:
				cmd_api(api_kb)
		elif cmd == "help":
			print_help()
		else:
			print(f"  Unknown command: '{cmd}'. Type 'help' for available commands.")


def main():
	"""Entry point."""
	parser = argparse.ArgumentParser(description="GodotVibe - Vibe Coding for Godot")
	parser.add_argument("command", nargs="?", default=None,
						help="Command: web (default), cli, new, templates, api")
	parser.add_argument("--host", default="127.0.0.1", help="Web server host")
	parser.add_argument("--port", type=int, default=8899, help="Web server port")
	args = parser.parse_args()

	if args.command is None or args.command == "web":
		cmd_web(host=args.host, port=args.port)
	elif args.command == "cli":
		interactive_mode()
	elif args.command == "new":
		runner = GodotRunner(str(GODOT_EXE))
		cmd_new(runner, None)
	elif args.command == "templates":
		cmd_templates()
	elif args.command == "api":
		print("  📚 Building API knowledge base...")
		api_kb = APIKnowledgeBase.build_from_xml(str(DOC_CLASSES_DIR))
		api_kb.save_to_cache(str(API_CACHE_DIR / "api_knowledge.json"))
		print(f"  ✅ Built and cached {len(api_kb.classes)} classes")
	else:
		print(f"  Unknown command: {args.command}")
		parser.print_help()


if __name__ == "__main__":
	main()
