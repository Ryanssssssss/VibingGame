# Godot 4.x Basics (NOT 3.x!)

## GDScript Rules
- GDScript uses **TAB** indentation (not spaces)
- `await` not `yield`
- `instantiate()` not `instance()`

## Renamed Classes (3.x → 4.x)
- `KinematicBody2D` → `CharacterBody2D`
- `Spatial` → `Node3D`
- `TileMap` → `TileMapLayer`
- `export` → `@export`
- `onready` → `@onready`

## Key API Changes
- `move_and_slide()` takes NO arguments — `velocity` is a property on CharacterBody2D/3D
- `CollisionShape2D`/`CollisionShape3D` MUST have a SubResource shape assigned
- `.tscn` files use `format=3`, root node has NO `parent` field, direct children have `parent="."`

## CharacterBody2D Pattern
```gdscript
extends CharacterBody2D
const SPEED = 300.0
const JUMP_VELOCITY = -400.0
func _physics_process(delta: float) -> void:
	if not is_on_floor():
		velocity += get_gravity() * delta
	if Input.is_action_just_pressed("ui_accept") and is_on_floor():
		velocity.y = JUMP_VELOCITY
	var direction := Input.get_axis("ui_left", "ui_right")
	if direction:
		velocity.x = direction * SPEED
	else:
		velocity.x = move_toward(velocity.x, 0, SPEED)
	move_and_slide()
```

## CharacterBody3D Pattern
```gdscript
extends CharacterBody3D
const SPEED = 5.0
const JUMP_VELOCITY = 4.5
func _physics_process(delta: float) -> void:
	if not is_on_floor():
		velocity += get_gravity() * delta
	if Input.is_action_just_pressed("ui_accept") and is_on_floor():
		velocity.y = JUMP_VELOCITY
	var input_dir := Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
	var direction := (transform.basis * Vector3(input_dir.x, 0, input_dir.y)).normalized()
	if direction:
		velocity.x = direction.x * SPEED
		velocity.z = direction.z * SPEED
	else:
		velocity.x = move_toward(velocity.x, 0, SPEED)
		velocity.z = move_toward(velocity.z, 0, SPEED)
	move_and_slide()
```
