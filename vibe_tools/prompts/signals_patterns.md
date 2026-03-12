# Signals & Common Patterns in Godot 4.x

## Connecting Signals
```gdscript
# Method 1: connect()
button.pressed.connect(_on_button_pressed)
area.body_entered.connect(_on_body_entered)

# Method 2: with lambda
button.pressed.connect(func(): print("clicked"))

# Method 3: with bind
timer.timeout.connect(_on_timeout.bind(enemy_id))

# Disconnect
button.pressed.disconnect(_on_button_pressed)
```

## Custom Signals
```gdscript
signal health_changed(new_health: int)
signal died

func take_damage(amount):
	health -= amount
	health_changed.emit(health)
	if health <= 0:
		died.emit()
```

## Scene Management
```gdscript
# Change scene
get_tree().change_scene_to_file("res://levels/level2.tscn")

# Instance a scene
var scene = preload("res://bullet.tscn")
var bullet = scene.instantiate()
bullet.position = position
get_parent().add_child(bullet)

# Remove node
queue_free()

# Pause
get_tree().paused = true  # node must have process_mode set
```

## Groups
```gdscript
# Add to group
add_to_group("enemies")

# Call all in group
get_tree().call_group("enemies", "take_damage", 10)

# Get nodes in group
var enemies = get_tree().get_nodes_in_group("enemies")
```

## Autoload (Singleton)
Use `edit_project_settings` to register:
```
[autoload]
GameManager="*res://game_manager.gd"
```
Then access anywhere: `GameManager.score += 1`

## Common Patterns

### State Machine
```gdscript
enum State { IDLE, RUN, JUMP, ATTACK }
var current_state = State.IDLE

func _physics_process(delta):
	match current_state:
		State.IDLE:
			_state_idle(delta)
		State.RUN:
			_state_run(delta)
		State.JUMP:
			_state_jump(delta)
```

### Object Pool
```gdscript
var pool: Array[Node] = []
func get_from_pool() -> Node:
	for obj in pool:
		if not obj.visible:
			obj.visible = true
			return obj
	var new_obj = scene.instantiate()
	pool.append(new_obj)
	add_child(new_obj)
	return new_obj
```

### Screen Shake
```gdscript
func shake(intensity: float = 5.0, duration: float = 0.3):
	var tween = create_tween()
	for i in range(int(duration / 0.05)):
		tween.tween_property($Camera2D, "offset", 
			Vector2(randf_range(-1,1), randf_range(-1,1)) * intensity, 0.05)
	tween.tween_property($Camera2D, "offset", Vector2.ZERO, 0.05)
```
