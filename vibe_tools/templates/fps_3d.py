"""3D First-Person Shooter template - FPS with WASD + mouse look."""

from __future__ import annotations

from vibe_tools.models import (
	SceneDesc, ScriptDesc, NodeDesc, ProjectSettings,
)
from vibe_tools.templates.base import GameTemplate


class FPS3DTemplate(GameTemplate):
	@property
	def name(self) -> str:
		return "fps_3d"

	@property
	def description(self) -> str:
		return "3D first-person game with WASD movement and mouse look"

	def get_scenes(self) -> list[SceneDesc]:
		main_scene = SceneDesc(
			filename="main.tscn",
			root=NodeDesc(
				name="Main",
				type="Node3D",
				children=[
					# World environment
					NodeDesc(
						name="WorldEnvironment",
						type="WorldEnvironment",
					),
					# Directional light (sun)
					NodeDesc(
						name="DirectionalLight3D",
						type="DirectionalLight3D",
						properties={
							"transform": 'Transform3D(0.866, -0.433, 0.25, 0, 0.5, 0.866, -0.5, -0.75, 0.433, 0, 10, 0)',
							"shadow_enabled": "true",
						},
					),
					# Floor
					NodeDesc(
						name="Floor",
						type="StaticBody3D",
						children=[
							NodeDesc(
								name="MeshInstance3D",
								type="MeshInstance3D",
							),
							NodeDesc(
								name="CollisionShape3D",
								type="CollisionShape3D",
							),
						],
					),
					# Some boxes for decoration
					_box("Box1", "Vector3(3, 0.5, -3)"),
					_box("Box2", "Vector3(-2, 0.5, -5)"),
					_box("Box3", "Vector3(0, 0.5, -8)"),
					_box("Box4", "Vector3(5, 0.5, -6)"),
					# Player
					NodeDesc(
						name="Player",
						type="CharacterBody3D",
						script="res://player.gd",
						properties={
							"transform": 'Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 0)',
						},
						children=[
							NodeDesc(
								name="CollisionShape3D",
								type="CollisionShape3D",
							),
							NodeDesc(
								name="Head",
								type="Node3D",
								children=[
									NodeDesc(
										name="Camera3D",
										type="Camera3D",
										properties={
											"current": "true",
										},
									),
								],
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
			extends="CharacterBody3D",
			raw_code='''extends CharacterBody3D

const SPEED = 5.0
const JUMP_VELOCITY = 4.5
const MOUSE_SENSITIVITY = 0.002
const GRAVITY = 9.8

@onready var head: Node3D = $Head

func _ready() -> void:
\tInput.mouse_mode = Input.MOUSE_MODE_CAPTURED

func _unhandled_input(event: InputEvent) -> void:
\tif event is InputEventMouseMotion:
\t\trotate_y(-event.relative.x * MOUSE_SENSITIVITY)
\t\thead.rotate_x(-event.relative.y * MOUSE_SENSITIVITY)
\t\thead.rotation.x = clamp(head.rotation.x, -PI / 2, PI / 2)

\tif event.is_action_pressed("ui_cancel"):
\t\tInput.mouse_mode = Input.MOUSE_MODE_VISIBLE

func _physics_process(delta: float) -> void:
\tif not is_on_floor():
\t\tvelocity.y -= GRAVITY * delta

\tif Input.is_action_just_pressed("ui_accept") and is_on_floor():
\t\tvelocity.y = JUMP_VELOCITY

\tvar input_dir := Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
\tvar direction := (transform.basis * Vector3(input_dir.x, 0, input_dir.y)).normalized()

\tif direction:
\t\tvelocity.x = direction.x * SPEED
\t\tvelocity.z = direction.z * SPEED
\telse:
\t\tvelocity.x = move_toward(velocity.x, 0, SPEED)
\t\tvelocity.z = move_toward(velocity.z, 0, SPEED)

\tmove_and_slide()
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


def _box(name: str, pos: str) -> NodeDesc:
	"""Create a box StaticBody3D node."""
	return NodeDesc(
		name=name,
		type="StaticBody3D",
		properties={"transform": f'Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, {pos.replace("Vector3(", "").replace(")", "")})'},
		children=[
			NodeDesc(
				name="MeshInstance3D",
				type="MeshInstance3D",
			),
			NodeDesc(
				name="CollisionShape3D",
				type="CollisionShape3D",
			),
		],
	)
