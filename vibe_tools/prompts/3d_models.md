# 3D Models in Godot 4.x (.glb/.gltf)

## How Godot Imports 3D Models
When a `.glb` or `.gltf` file is in the project, Godot auto-imports it as a **PackedScene**.
You can instance it like any other scene.

**IMPORTANT**: After uploading `.glb` files, Godot must run `--headless --import` to generate
the `.godot/imported/` cache. The `run_project` tool does this automatically.

## Using 3D Models in .tscn (PREFERRED for generate_project)
Use the `instance` field on a node to instance a `.glb` file directly in the scene tree:

```json
{
  "name": "PlayerModel",
  "type": "Node3D",
  "instance": "res://assets/character.glb",
  "properties": {
    "position": "Vector3(0, 0, 0)",
    "scale": "Vector3(1, 1, 1)"
  }
}
```

The scene generator will create:
```
[ext_resource type="PackedScene" path="res://assets/character.glb" id="1_abc"]

[node name="PlayerModel" parent="." instance=ExtResource("1_abc")]
position = Vector3(0, 0, 0)
scale = Vector3(1, 1, 1)
```

## Using 3D Models in GDScript (for dynamic spawning)
```gdscript
var model_scene: PackedScene = preload("res://assets/character.glb")
var instance = model_scene.instantiate()
add_child(instance)
instance.position = Vector3(0, 0, 0)
```

## Playing Animations from .glb

**CRITICAL: NEVER guess or invent animation names!** Use `list_assets` metadata for actual names.

GLB files with animations have an `AnimationPlayer` child node inside:
```gdscript
# Find the AnimationPlayer inside the instanced model
@onready var model: Node3D = $Model
var anim_player: AnimationPlayer

func _ready() -> void:
	anim_player = model.find_child("AnimationPlayer")
	if anim_player:
		# List all available animations (RECOMMENDED)
		for anim_name in anim_player.get_animation_list():
			print("Available animation: ", anim_name)
		# Play the first animation as default
		var anims = anim_player.get_animation_list()
		if anims.size() > 0:
			anim_player.play(anims[0])
```

**WARNING**: Animation names from `list_assets` metadata are extracted from the raw glTF file.
Godot's importer may modify them (e.g., strip `Armature|` prefix, rename duplicates).
If an animation fails to play, always fall back to listing animations at runtime with
`get_animation_list()` and use the actual Godot-imported names.

**SAFE animation switching pattern** (handles unknown names gracefully):
```gdscript
func play_animation(name: String) -> void:
	if anim_player and anim_player.has_animation(name):
		if anim_player.current_animation != name:
			anim_player.play(name)

func find_animation_containing(keyword: String) -> String:
	if not anim_player:
		return ""
	for anim_name in anim_player.get_animation_list():
		if keyword.to_lower() in anim_name.to_lower():
			return anim_name
	return ""
```

## Common Patterns

### Static 3D Object (decoration, obstacle)
```json
{
  "name": "Tree",
  "type": "Node3D",
  "instance": "res://assets/tree.glb",
  "properties": {"position": "Vector3(5, 0, 3)"}
}
```

### 3D Character with GLB Model (Third-Person)

**CORRECT structure** — Model is a SIBLING of CollisionShape3D and CameraPivot:
```json
{
  "name": "Player",
  "type": "CharacterBody3D",
  "script": "res://player.gd",
  "properties": {"transform": "Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 0)"},
  "children": [
    {
      "name": "Model",
      "type": "Node3D",
      "instance": "res://assets/character.glb",
      "properties": {
        "transform": "Transform3D(-1, 0, 0, 0, 1, 0, 0, 0, -1, 0, 0, 0)",
        "scale": "Vector3(1, 1, 1)"
      }
    },
    {
      "name": "CollisionShape3D",
      "type": "CollisionShape3D"
    },
    {
      "name": "CameraPivot",
      "type": "Node3D",
      "children": [
        {
          "name": "SpringArm3D",
          "type": "SpringArm3D",
          "properties": {
            "transform": "Transform3D(1, 0, 0, 0, 0.966, 0.259, 0, -0.259, 0.966, 0, 0, 0)",
            "spring_length": "5.0"
          },
          "children": [
            {"name": "Camera3D", "type": "Camera3D", "properties": {"current": "true"}}
          ]
        }
      ]
    }
  ]
}
```

**Key points:**
- `Model` transform `Transform3D(-1, 0, 0, 0, 1, 0, 0, 0, -1, 0, 0, 0)` = rotated 180° around Y so model faces AWAY from camera
- `SpringArm3D` tilted ~15° downward so camera looks slightly down at the player
- CollisionShape3D, CameraPivot are siblings of Model — NOT children of Model

### Third-Person Player Script (camera-relative movement)
```gdscript
extends CharacterBody3D

const SPEED = 5.0
const JUMP_VELOCITY = 4.5
const MOUSE_SENSITIVITY = 0.003
const GRAVITY = 9.8

@onready var camera_pivot: Node3D = $CameraPivot
@onready var model: Node3D = $Model

var anim_player: AnimationPlayer
var idle_anim: String = ""
var move_anim: String = ""
var jump_anim: String = ""

func _ready() -> void:
	Input.mouse_mode = Input.MOUSE_MODE_CAPTURED
	# Discover animations at runtime — NEVER hardcode names
	anim_player = model.find_child("AnimationPlayer")
	if anim_player:
		for anim_name in anim_player.get_animation_list():
			var low = anim_name.to_lower()
			if "idle" in low and idle_anim == "":
				idle_anim = anim_name
			elif ("walk" in low or "run" in low or "jog" in low) and move_anim == "":
				move_anim = anim_name
			elif "jump" in low and jump_anim == "":
				jump_anim = anim_name
		# Fallback: use first animation as idle
		if idle_anim == "" and anim_player.get_animation_list().size() > 0:
			idle_anim = anim_player.get_animation_list()[0]
		if idle_anim != "":
			anim_player.play(idle_anim)

func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseMotion:
		camera_pivot.rotate_y(-event.relative.x * MOUSE_SENSITIVITY)
		camera_pivot.rotation.x = clamp(
			camera_pivot.rotation.x - event.relative.y * MOUSE_SENSITIVITY,
			-PI / 4, PI / 6
		)
	if event.is_action_pressed("ui_cancel"):
		Input.mouse_mode = Input.MOUSE_MODE_VISIBLE

func _physics_process(delta: float) -> void:
	if not is_on_floor():
		velocity.y -= GRAVITY * delta

	if Input.is_action_just_pressed("ui_accept") and is_on_floor():
		velocity.y = JUMP_VELOCITY
		_play_anim(jump_anim)

	# Movement direction relative to CAMERA, not player body
	var input_dir := Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
	var cam_basis := camera_pivot.global_transform.basis
	var direction := (cam_basis * Vector3(input_dir.x, 0, input_dir.y)).normalized()
	direction.y = 0

	if direction:
		velocity.x = direction.x * SPEED
		velocity.z = direction.z * SPEED
		# Rotate only the Model node to face movement direction (NOT the whole body)
		var target_angle := atan2(direction.x, direction.z)
		model.rotation.y = lerp_angle(model.rotation.y, target_angle, 0.15)
		_play_anim(move_anim)
	else:
		velocity.x = move_toward(velocity.x, 0, SPEED)
		velocity.z = move_toward(velocity.z, 0, SPEED)
		if is_on_floor():
			_play_anim(idle_anim)

	move_and_slide()

func _play_anim(anim_name: String) -> void:
	if anim_player and anim_name != "" and anim_player.current_animation != anim_name:
		anim_player.play(anim_name)
```

### Adjusting Scale
3D models often need scale adjustment. If `list_assets` reports metadata, use it:
- If model is too big: `"scale": "Vector3(0.01, 0.01, 0.01)"` (e.g., Blender default export)
- If model is too small: `"scale": "Vector3(10, 10, 10)"`

## Key Rules
1. **NEVER** use `type="PackedScene"` as a node type — the validator rejects this
2. Use `instance` field in the node JSON to reference `.glb`/`.gltf` files
3. The instanced node inherits the root type of the `.glb` (usually `Node3D`)
4. Properties like `position`, `scale`, `rotation` can override the instanced scene's defaults
5. Scripts can be attached to the parent node (e.g., `CharacterBody3D`) that contains the model as a child
6. Use `preload()` / `load()` in GDScript for dynamic spawning at runtime
7. **NEVER** add `AnimationPlayer` or `AnimationTree` as children of an instanced `.glb` node in `.tscn` — the GLB already contains its own `AnimationPlayer`. Adding a duplicate will shadow/conflict with the built-in one. Access the GLB's built-in AnimationPlayer via GDScript: `$Model/AnimationPlayer` or `model.find_child("AnimationPlayer")`
8. **NEVER** add child nodes to an instanced node in `.tscn` — instanced scenes already contain their own subtree. If you need to add siblings (e.g., CollisionShape3D), add them as children of the **parent** node, not the instanced node
9. **NEVER** guess animation names — use `list_assets` metadata or discover at runtime with `get_animation_list()`. Animation names like "Idle_Loop", "Jog_Fwd_Loop" differ per model; never assume "idle", "walk", "run" etc.
10. **Third-person camera**: Movement MUST be camera-relative (use `camera_pivot.global_transform.basis`). Rotate only the `Model` child's `rotation.y` for facing direction — do NOT rotate the CharacterBody3D itself (that would rotate the camera orbit too)
