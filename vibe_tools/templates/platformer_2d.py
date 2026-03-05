"""2D Platformer template - side-scrolling platform game."""

from __future__ import annotations

from vibe_tools.models import (
	SceneDesc, ScriptDesc, NodeDesc, ProjectSettings, InputEvent,
	ExportVar, OnReadyVar, FunctionDesc, SubResource,
)
from vibe_tools.templates.base import GameTemplate


class Platformer2DTemplate(GameTemplate):
	@property
	def name(self) -> str:
		return "platformer_2d"

	@property
	def description(self) -> str:
		return "2D side-scrolling platformer with player, platforms, and camera"

	def get_scenes(self) -> list[SceneDesc]:
		# Main scene
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
					# Ground platform
					NodeDesc(
						name="Ground",
						type="StaticBody2D",
						properties={
							"position": "Vector2(576, 600)",
						},
						children=[
							NodeDesc(
								name="CollisionShape2D",
								type="CollisionShape2D",
							),
							NodeDesc(
								name="ColorRect",
								type="ColorRect",
								properties={
									"offset_left": "-576.0",
									"offset_top": "-16.0",
									"offset_right": "576.0",
									"offset_bottom": "16.0",
									"color": 'Color(0.365, 0.529, 0.318, 1)',
								},
							),
						],
					),
					# Floating platforms
					NodeDesc(
						name="Platform1",
						type="StaticBody2D",
						properties={
							"position": "Vector2(300, 450)",
						},
						children=[
							NodeDesc(
								name="CollisionShape2D",
								type="CollisionShape2D",
							),
							NodeDesc(
								name="ColorRect",
								type="ColorRect",
								properties={
									"offset_left": "-80.0",
									"offset_top": "-8.0",
									"offset_right": "80.0",
									"offset_bottom": "8.0",
									"color": 'Color(0.467, 0.392, 0.282, 1)',
								},
							),
						],
					),
					NodeDesc(
						name="Platform2",
						type="StaticBody2D",
						properties={
							"position": "Vector2(700, 350)",
						},
						children=[
							NodeDesc(
								name="CollisionShape2D",
								type="CollisionShape2D",
							),
							NodeDesc(
								name="ColorRect",
								type="ColorRect",
								properties={
									"offset_left": "-80.0",
									"offset_top": "-8.0",
									"offset_right": "80.0",
									"offset_bottom": "8.0",
									"color": 'Color(0.467, 0.392, 0.282, 1)',
								},
							),
						],
					),
					# Player
					NodeDesc(
						name="Player",
						type="CharacterBody2D",
						script="res://player.gd",
						properties={
							"position": "Vector2(576, 500)",
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
									"offset_left": "-16.0",
									"offset_top": "-32.0",
									"offset_right": "16.0",
									"offset_bottom": "0.0",
									"color": 'Color(0.31, 0.565, 0.847, 1)',
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

const SPEED = 300.0
const JUMP_VELOCITY = -500.0
const GRAVITY = 980.0

func _physics_process(delta: float) -> void:
\tif not is_on_floor():
\t\tvelocity.y += GRAVITY * delta

\tif Input.is_action_just_pressed("ui_accept") and is_on_floor():
\t\tvelocity.y = JUMP_VELOCITY

\tvar direction := Input.get_axis("ui_left", "ui_right")
\tif direction:
\t\tvelocity.x = direction * SPEED
\telse:
\t\tvelocity.x = move_toward(velocity.x, 0, SPEED)

\tmove_and_slide()

\t# Keep camera following player
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
