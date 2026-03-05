"""3D Third-Person template - TPS with SpringArm3D camera."""

from __future__ import annotations

from vibe_tools.models import (
	SceneDesc, ScriptDesc, NodeDesc, ProjectSettings,
)
from vibe_tools.templates.base import GameTemplate


class TPS3DTemplate(GameTemplate):
	@property
	def name(self) -> str:
		return "tps_3d"

	@property
	def description(self) -> str:
		return "3D third-person game with orbit camera and character movement"

	def get_scenes(self) -> list[SceneDesc]:
		main_scene = SceneDesc(
			filename="main.tscn",
			root=NodeDesc(
				name="Main",
				type="Node3D",
				children=[
					# Directional light
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
							NodeDesc(name="MeshInstance3D", type="MeshInstance3D"),
							NodeDesc(name="CollisionShape3D", type="CollisionShape3D"),
						],
					),
					# Player
					NodeDesc(
						name="Player",
						type="CharacterBody3D",
						script="res://player.gd",
						properties={
							"transform": 'Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 0)',
						},
						children=[
							NodeDesc(name="CollisionShape3D", type="CollisionShape3D"),
							NodeDesc(
								name="PlayerModel",
								type="MeshInstance3D",
							),
							NodeDesc(
								name="CameraPivot",
								type="Node3D",
								children=[
									NodeDesc(
										name="SpringArm3D",
										type="SpringArm3D",
										properties={
											"spring_length": "5.0",
										},
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
					# Some obstacles
					NodeDesc(
						name="Obstacles",
						type="Node3D",
						children=[
							_obstacle("Pillar1", "Vector3(4, 1.5, -4)"),
							_obstacle("Pillar2", "Vector3(-3, 1.5, -6)"),
							_obstacle("Pillar3", "Vector3(1, 1.5, -9)"),
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
const MOUSE_SENSITIVITY = 0.003
const GRAVITY = 9.8

@onready var camera_pivot: Node3D = $CameraPivot

func _ready() -> void:
\tInput.mouse_mode = Input.MOUSE_MODE_CAPTURED

func _unhandled_input(event: InputEvent) -> void:
\tif event is InputEventMouseMotion:
\t\tcamera_pivot.rotate_y(-event.relative.x * MOUSE_SENSITIVITY)
\t\tcamera_pivot.rotation.x = clamp(
\t\t\tcamera_pivot.rotation.x - event.relative.y * MOUSE_SENSITIVITY,
\t\t\t-PI / 4, PI / 6
\t\t)

\tif event.is_action_pressed("ui_cancel"):
\t\tInput.mouse_mode = Input.MOUSE_MODE_VISIBLE

func _physics_process(delta: float) -> void:
\tif not is_on_floor():
\t\tvelocity.y -= GRAVITY * delta

\tif Input.is_action_just_pressed("ui_accept") and is_on_floor():
\t\tvelocity.y = JUMP_VELOCITY

\tvar input_dir := Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
\tvar cam_basis := camera_pivot.global_transform.basis
\tvar direction := (cam_basis * Vector3(input_dir.x, 0, input_dir.y)).normalized()
\tdirection.y = 0

\tif direction:
\t\tvelocity.x = direction.x * SPEED
\t\tvelocity.z = direction.z * SPEED
\t\t# Rotate player to face movement direction
\t\tvar target_angle := atan2(direction.x, direction.z)
\t\trotation.y = lerp_angle(rotation.y, target_angle, 0.15)
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


def _obstacle(name: str, pos: str) -> NodeDesc:
	"""Create an obstacle StaticBody3D."""
	coords = pos.replace("Vector3(", "").replace(")", "")
	return NodeDesc(
		name=name,
		type="StaticBody3D",
		properties={"transform": f'Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, {coords})'},
		children=[
			NodeDesc(name="MeshInstance3D", type="MeshInstance3D"),
			NodeDesc(name="CollisionShape3D", type="CollisionShape3D"),
		],
	)
