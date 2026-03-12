# Navigation / Pathfinding in Godot 4.x

## 2D Navigation
```gdscript
# Setup navigation region
var region = NavigationRegion2D.new()
var navpoly = NavigationPolygon.new()
# Define walkable area
var outline = PackedVector2Array([
	Vector2(0, 0), Vector2(800, 0), Vector2(800, 600), Vector2(0, 600)
])
navpoly.add_outline(outline)
navpoly.make_polygons_from_outlines()
region.navigation_polygon = navpoly
add_child(region)

# Agent that follows paths
var agent = NavigationAgent2D.new()
agent.path_desired_distance = 4.0
agent.target_desired_distance = 4.0
character.add_child(agent)

# Set target and move
agent.target_position = target_pos
var next = agent.get_next_path_position()
var direction = (next - character.global_position).normalized()
character.velocity = direction * SPEED
character.move_and_slide()
```

## 3D Navigation
```gdscript
var region = NavigationRegion3D.new()
var navmesh = NavigationMesh.new()
# Bake navigation mesh from geometry
region.navigation_mesh = navmesh
region.bake_navigation_mesh()
add_child(region)

var agent = NavigationAgent3D.new()
agent.path_desired_distance = 0.5
agent.target_desired_distance = 0.5
character.add_child(agent)

# Move toward target
agent.target_position = target_pos
var next = agent.get_next_path_position()
var direction = (next - character.global_transform.origin).normalized()
character.velocity = direction * SPEED
character.move_and_slide()
```

## Signals
- `NavigationAgent.velocity_computed(safe_velocity)` — use with avoidance
- `NavigationAgent.navigation_finished` — arrived at target
- `NavigationAgent.path_changed` — path was recalculated
