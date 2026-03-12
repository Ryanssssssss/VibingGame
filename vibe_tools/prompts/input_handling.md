# Input Handling in Godot 4.x

## Input Map (project.godot)
Built-in actions: `ui_accept`, `ui_cancel`, `ui_left`, `ui_right`, `ui_up`, `ui_down`, `ui_focus_next`, `ui_focus_prev`

### Custom Actions (add via edit_project_settings)
```
[input]
move_left={
"deadzone": 0.5,
"events": [Object(InputEventKey,"resource_local_to_scene":false,"resource_name":"","device":-1,"window_id":0,"alt_pressed":false,"shift_pressed":false,"ctrl_pressed":false,"meta_pressed":false,"pressed":false,"keycode":65,"physical_keycode":0,"key_label":0,"unicode":97,"location":0,"echo":false,"script":null)]
}
```

## Polling (in _process or _physics_process)
```gdscript
# Digital (pressed/released)
if Input.is_action_pressed("move_left"):
	velocity.x = -SPEED
if Input.is_action_just_pressed("jump"):
	velocity.y = JUMP_VELOCITY

# Axis (returns -1 to 1)
var h = Input.get_axis("move_left", "move_right")
var v = Input.get_axis("move_up", "move_down")
var direction = Vector2(h, v).normalized()

# 2D vector (4 actions → Vector2)
var input = Input.get_vector("move_left", "move_right", "move_up", "move_down")

# Mouse
var mouse_pos = get_global_mouse_position()
if Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT):
	pass
```

## Event-Based (_input / _unhandled_input)
```gdscript
func _input(event):
	if event is InputEventKey and event.pressed:
		if event.keycode == KEY_ESCAPE:
			get_tree().quit()
	
	if event is InputEventMouseButton:
		if event.button_index == MOUSE_BUTTON_LEFT and event.pressed:
			shoot()
	
	if event is InputEventMouseMotion:
		rotate_y(-event.relative.x * 0.005)

func _unhandled_input(event):
	# Only fires if no GUI consumed the event
	if event.is_action_pressed("interact"):
		interact()
```

## Touch Input
```gdscript
func _input(event):
	if event is InputEventScreenTouch:
		if event.pressed:
			touch_start = event.position
	if event is InputEventScreenDrag:
		var drag = event.relative
```

## Mouse Capture (for FPS)
```gdscript
func _ready():
	Input.mouse_mode = Input.MOUSE_MODE_CAPTURED

func _input(event):
	if event is InputEventKey and event.keycode == KEY_ESCAPE:
		Input.mouse_mode = Input.MOUSE_MODE_VISIBLE
```
