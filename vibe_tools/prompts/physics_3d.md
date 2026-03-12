# 3D Physics in Godot 4.x

## Body Types
- `StaticBody3D` — immovable (terrain, walls)
- `CharacterBody3D` — player/NPC controlled (use `move_and_slide()`)
- `RigidBody3D` — physics-simulated
- `Area3D` — trigger zones

## Collision Setup
```gdscript
var body = StaticBody3D.new()
var shape = CollisionShape3D.new()
shape.shape = BoxShape3D.new()
shape.shape.size = Vector3(2, 0.5, 2)
body.add_child(shape)
```

## Available Shapes
- `BoxShape3D` — props: `size: Vector3`
- `SphereShape3D` — props: `radius: float`
- `CapsuleShape3D` — props: `radius: float`, `height: float`
- `CylinderShape3D` — props: `radius: float`, `height: float`
- `WorldBoundaryShape3D` — infinite plane
- `ConvexPolygonShape3D` — set `points: PackedVector3Array`
- `ConcavePolygonShape3D` — set `faces: PackedVector3Array`

## 3D Movement Pattern
```gdscript
extends CharacterBody3D
const SPEED = 5.0
const JUMP_VELOCITY = 4.5

func _physics_process(delta):
	if not is_on_floor():
		velocity += get_gravity() * delta
	if Input.is_action_just_pressed("ui_accept") and is_on_floor():
		velocity.y = JUMP_VELOCITY
	var input_dir = Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
	var direction = (transform.basis * Vector3(input_dir.x, 0, input_dir.y)).normalized()
	if direction:
		velocity.x = direction.x * SPEED
		velocity.z = direction.z * SPEED
	else:
		velocity.x = move_toward(velocity.x, 0, SPEED)
		velocity.z = move_toward(velocity.z, 0, SPEED)
	move_and_slide()
```

## Camera Setup
```gdscript
var camera = Camera3D.new()
camera.position = Vector3(0, 5, 10)
camera.look_at(Vector3.ZERO)
camera.fov = 70.0
```

## Lighting
```gdscript
var sun = DirectionalLight3D.new()
sun.rotation_degrees = Vector3(-45, 30, 0)
sun.shadow_enabled = true

var point = OmniLight3D.new()
point.position = Vector3(0, 3, 0)
point.omni_range = 10.0
```
