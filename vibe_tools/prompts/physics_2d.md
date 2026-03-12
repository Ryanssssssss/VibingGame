# 2D Physics in Godot 4.x

## Body Types
- `StaticBody2D` — immovable (walls, floors, platforms)
- `CharacterBody2D` — player/NPC controlled movement (use `move_and_slide()`)
- `RigidBody2D` — physics-simulated (gravity, forces, collisions)
- `Area2D` — trigger zones (no physics, just detection)

## Collision Setup
Every physics body MUST have a `CollisionShape2D` child with a shape:
```gdscript
var body = StaticBody2D.new()
var shape = CollisionShape2D.new()
shape.shape = RectangleShape2D.new()
shape.shape.size = Vector2(64, 16)
body.add_child(shape)
```

## Available Shapes
- `RectangleShape2D` — props: `size: Vector2`
- `CircleShape2D` — props: `radius: float`
- `CapsuleShape2D` — props: `radius: float`, `height: float`
- `WorldBoundaryShape2D` — infinite plane
- `SegmentShape2D` — line segment
- `ConvexPolygonShape2D` — convex polygon (set `points`)
- `ConcavePolygonShape2D` — concave polygon (set `segments`)

## Collision Layers & Masks
- `collision_layer`: what layer this body IS ON
- `collision_mask`: what layers this body DETECTS
```gdscript
# Player on layer 1, detects layers 1,2,3
player.collision_layer = 1
player.collision_mask = 0b111  # layers 1-3
```

## Signals
- `Area2D.body_entered(body)` / `body_exited(body)` — physics body enters/exits
- `Area2D.area_entered(area)` / `area_exited(area)` — another Area2D enters/exits
- `RigidBody2D.body_entered(body)` — with `contact_monitor = true` and `max_contacts_reported > 0`

## RigidBody2D Usage
```gdscript
var rb = RigidBody2D.new()
rb.gravity_scale = 1.0
rb.mass = 1.0
# Apply forces:
rb.apply_central_impulse(Vector2(100, -200))  # instant
rb.apply_central_force(Vector2(0, -50))       # continuous
# Lock rotation:
rb.lock_rotation = true
```

## Raycasting
```gdscript
var ray = RayCast2D.new()
ray.target_position = Vector2(0, 100)  # downward 100px
ray.collision_mask = 1
add_child(ray)
# In _physics_process:
if ray.is_colliding():
	var hit_point = ray.get_collision_point()
	var hit_normal = ray.get_collision_normal()
	var hit_object = ray.get_collider()
```
