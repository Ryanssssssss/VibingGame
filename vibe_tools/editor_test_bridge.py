"""Bundled deterministic frame-state machine; no coroutine stack survives scene changes."""

BRIDGE = r'''extends Node

var spec: Dictionary = {}
var result := {"status": "passed", "summary": "", "observations": [], "screenshots": []}
var values: Dictionary = {}
var output: String
var mouse := Vector2.ZERO
var cursor := 0
var delay_frames := 3
var attempts := 0
var capturing := false
var observation: Dictionary = {}

func _ready() -> void:
    process_mode = Node.PROCESS_MODE_ALWAYS
    spec = JSON.parse_string(FileAccess.get_file_as_string("res://__vibe_test/case.json"))
    output = spec["output"]

func _save() -> void:
    var file := FileAccess.open(output.path_join("runtime.json"), FileAccess.WRITE)
    if file:
        file.store_string(JSON.stringify(result))

func _fail(message: String, status: String = "blocked") -> void:
    result["status"] = status
    result["summary"] = message

func _finish() -> void:
    set_physics_process(false)
    result["complete"] = true
    _save()
    get_tree().quit()

func _node(path: String) -> Node:
    if path.begins_with("/root"):
        return get_node_or_null(NodePath(path))
    var scene := get_tree().current_scene
    if scene == null:
        return null
    return scene if path == "." else scene.get_node_or_null(NodePath(path))

func _read(step: Dictionary) -> Variant:
    var target := _node(step["node"])
    if step.get("compare") == "exists":
        return target != null
    if target == null:
        _fail("找不到测试节点：" + step["node"])
        return null
    var property: String = step["property"]
    var found := false
    for info in target.get_property_list():
        if str(info["name"]) == property.get_slice(":", 0):
            found = true
            break
    if not found:
        _fail("找不到测试属性：" + property)
        return null
    var value: Variant = target.get_indexed(NodePath(property))
    if value is Vector2 or value is Vector2i:
        return [value.x, value.y]
    if value is Vector3 or value is Vector3i:
        return [value.x, value.y, value.z]
    if value is Color:
        return [value.r, value.g, value.b, value.a]
    if value is StringName or value is NodePath:
        return str(value)
    return value

func _matches(actual: Variant, expected: Variant, step: Dictionary) -> bool:
    match step.get("compare", "eq"):
        "eq", "exists": return actual == expected
        "ne": return actual != expected
        "contains": return str(expected) in str(actual)
        "gt", "ge", "lt", "le", "near":
            if not (actual is float or actual is int) or not (expected is float or expected is int):
                _fail("数值比较需要数值属性及期望值")
                return false
            match step["compare"]:
                "gt": return actual > expected
                "ge": return actual >= expected
                "lt": return actual < expected
                "le": return actual <= expected
                "near": return abs(float(actual) - float(expected)) <= float(step.get("tolerance", 0.01))
    return false

func _capture(diagnostic: bool) -> void:
    var picture := get_viewport().get_texture().get_image()
    var name := "step_%03d.png" % (cursor + 1000 if diagnostic else cursor)
    if picture == null or picture.is_empty() or picture.save_png(output.path_join(name)) != OK:
        if not diagnostic:
            _fail("截图采集失败")
    else:
        result["screenshots"].append(name)
        if not diagnostic:
            observation["screenshot"] = name
    capturing = false
    if diagnostic:
        _finish()
    else:
        _record()

func _record() -> void:
    observation["status"] = "pending" if observation["step"]["op"] == "visual" and result["status"] == "passed" else result["status"]
    result["observations"].append(observation)
    _save()
    if result["status"] != "passed":
        if DisplayServer.get_name() != "headless":
            capturing = true
            RenderingServer.frame_post_draw.connect(_capture.bind(true), CONNECT_ONE_SHOT)
        else:
            _finish()
        return
    cursor += 1
    attempts = 0

func _physics_process(_delta: float) -> void:
    if capturing:
        return
    if delay_frames > 0:
        delay_frames -= 1
        return
    if cursor >= spec["steps"].size():
        _finish()
        return
    var step: Dictionary = spec["steps"][cursor]
    observation = {"index": cursor, "step": step, "status": "passed"}
    match step["op"]:
        "load_scene":
            if not ResourceLoader.exists(step["scene"]):
                _fail("测试场景不存在：" + step["scene"])
            elif get_tree().change_scene_to_file(step["scene"]) != OK:
                _fail("无法加载场景：" + step["scene"], "failed")
            delay_frames = 3
        "action":
            if not InputMap.has_action(step["action"]):
                _fail("InputMap 中不存在动作：" + step["action"])
            else:
                var event := InputEventAction.new()
                event.action = step["action"]
                event.pressed = step.get("pressed", true)
                event.strength = 1.0 if event.pressed else 0.0
                Input.parse_input_event(event)
        "key":
            var code := OS.find_keycode_from_string(step["key"])
            if code == 0:
                _fail("未知按键：" + step["key"])
            else:
                var event := InputEventKey.new()
                event.keycode = code
                event.physical_keycode = code
                event.pressed = step.get("pressed", true)
                Input.parse_input_event(event)
        "mouse_move":
            var event := InputEventMouseMotion.new()
            var point: Array = step["position"]
            event.position = Vector2(point[0], point[1])
            event.global_position = event.position
            event.relative = event.position - mouse
            mouse = event.position
            get_viewport().push_input(event, true)
        "mouse_button":
            var event := InputEventMouseButton.new()
            event.position = mouse
            event.global_position = mouse
            event.button_index = int(step.get("button", 1))
            event.pressed = step.get("pressed", true)
            get_viewport().push_input(event, true)
        "wait":
            delay_frames = int(step.get("frames", 1))
        "read":
            var actual: Variant = _read(step)
            values[step["store"]] = actual
            observation["actual"] = actual
        "assert", "wait_until":
            var expected: Variant = step.get("value")
            var reference: String = step.get("reference", "")
            if not reference.is_empty():
                if not values.has(reference):
                    _fail("不存在先前读取的值：" + reference)
                expected = values.get(reference)
            var actual: Variant = _read(step)
            var matches := _matches(actual, expected, step)
            observation["actual"] = actual
            observation["expected"] = expected
            if not matches and result["status"] == "passed":
                attempts += 1
                if step["op"] == "wait_until" and attempts < int(step.get("frames", 1)):
                    return
                _fail("断言失败：%s.%s 实际=%s，期望 %s %s" % [step["node"], step["property"], str(actual), step.get("compare", "eq"), str(expected)], "failed")
        "screenshot", "visual":
            if step["op"] == "visual":
                observation["status"] = "pending"
            if DisplayServer.get_name() == "headless":
                _fail("无渲染显示服务，无法采集截图")
            else:
                capturing = true
                RenderingServer.frame_post_draw.connect(_capture.bind(false), CONNECT_ONE_SHOT)
                return
    _record()
'''
