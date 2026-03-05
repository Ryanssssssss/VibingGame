"""2D Top-Down template - overhead view game with 8-directional movement."""

from __future__ import annotations

from vibe_tools.models import (
	SceneDesc, ScriptDesc, NodeDesc, ProjectSettings,
)
from vibe_tools.templates.base import GameTemplate


class TopDown2DTemplate(GameTemplate):
	@property
	def name(self) -> str:
		return "topdown_2d"

	@property
	def description(self) -> str:
		return "2D top-down game with 8-directional movement and collision"

	def get_scenes(self) -> list[SceneDesc]:
		main_scene = SceneDesc(
			filename="main.tscn",
			root=NodeDesc(
				name="Main",
				type="Node2D",
				children=[
					# Camera
					NodeDesc(
						name="Camera2D",
						type="Camera2D",
						properties={
							"zoom": "Vector2(2, 2)",
						},
					),
					# Background
					NodeDesc(
						name="Background",
						type="ColorRect",
						properties={
							"offset_left": "-1000.0",
							"offset_top": "-1000.0",
							"offset_right": "2000.0",
							"offset_bottom": "2000.0",
							"color": 'Color(0.176, 0.204, 0.255, 1)',
						},
					),
					# Walls
					NodeDesc(
						name="Walls",
						type="Node2D",
						children=[
							_wall("WallTop", "Vector2(576, 50)", "Vector2(1100, 20)"),
							_wall("WallBottom", "Vector2(576, 600)", "Vector2(1100, 20)"),
							_wall("WallLeft", "Vector2(26, 324)", "Vector2(20, 580)"),
							_wall("WallRight", "Vector2(1126, 324)", "Vector2(20, 580)"),
							_wall("Obstacle1", "Vector2(400, 300)", "Vector2(64, 64)"),
							_wall("Obstacle2", "Vector2(700, 200)", "Vector2(48, 96)"),
						],
					),
					# Player
					NodeDesc(
						name="Player",
						type="CharacterBody2D",
						script="res://player.gd",
						properties={
							"position": "Vector2(576, 324)",
						},
						children=[
							NodeDesc(
								name="CollisionShape2D",
								type="CollisionShape2D",
							),
							NodeDesc(
								name="Sprite",
								type="ColorRect",
								properties={
									"offset_left": "-12.0",
									"offset_top": "-12.0",
									"offset_right": "12.0",
									"offset_bottom": "12.0",
									"color": 'Color(0.259, 0.624, 0.886, 1)',
								},
							),
						],
					),
				],
			),
		)

		return [main_scene]

	def get_scripts(self) -> list[ScriptDesc]:
		player_script = ScriptDesc(
			filename="player.gd",
			extends="CharacterBody2D",
			raw_code='''extends CharacterBody2D

const SPEED = 200.0

func _physics_process(_delta: float) -> void:
\tvar input_dir := Vector2.ZERO
\tinput_dir.x = Input.get_axis("ui_left", "ui_right")
\tinput_dir.y = Input.get_axis("ui_up", "ui_down")

\tif input_dir.length() > 0:
\t\tinput_dir = input_dir.normalized()

\tvelocity = input_dir * SPEED
\tmove_and_slide()

\t# Camera follow
\tvar camera = get_parent().get_node("Camera2D")
\tif camera:
\t\tcamera.position = position
''',
		)

		return [player_script]

	def get_settings(self) -> ProjectSettings:
		return ProjectSettings(
			main_scene="res://main.tscn",
			window_width=1152,
			window_height=648,
			stretch_mode="canvas_items",
			stretch_aspect="expand",
		)


def _wall(name: str, pos: str, size: str) -> NodeDesc:
	"""Create a wall StaticBody2D node."""
	return NodeDesc(
		name=name,
		type="StaticBody2D",
		properties={"position": pos},
		children=[
			NodeDesc(
				name="CollisionShape2D",
				type="CollisionShape2D",
			),
			NodeDesc(
				name="ColorRect",
				type="ColorRect",
				properties={
					"offset_left": f"-{size.split(',')[0].strip().replace('Vector2(', '').strip()}.0",
					"offset_top": f"-{size.split(',')[1].strip().replace(')', '').strip()}.0",
					"offset_right": f"{size.split(',')[0].strip().replace('Vector2(', '').strip()}.0",
					"offset_bottom": f"{size.split(',')[1].strip().replace(')', '').strip()}.0",
					"color": 'Color(0.447, 0.463, 0.502, 1)',
				},
			),
		],
	)
