# Animation in Godot 4.x

## AnimationPlayer
Node-based keyframe animation system. Can animate ANY property of ANY node.

```gdscript
var anim_player = AnimationPlayer.new()
add_child(anim_player)

# Create animation library
var lib = AnimationLibrary.new()

# Create an animation
var anim = Animation.new()
anim.length = 1.0
anim.loop_mode = Animation.LOOP_LINEAR

# Add a track (animate Sprite2D position)
var track_idx = anim.add_track(Animation.TYPE_VALUE)
anim.track_set_path(track_idx, "Sprite2D:position")
anim.track_insert_key(track_idx, 0.0, Vector2(0, 0))
anim.track_insert_key(track_idx, 0.5, Vector2(100, 0))
anim.track_insert_key(track_idx, 1.0, Vector2(0, 0))

lib.add_animation("bounce", anim)
anim_player.add_animation_library("", lib)
anim_player.play("bounce")
```

## AnimatedSprite2D
For frame-based sprite animation (sprite sheets, individual frames).

```gdscript
var anim_sprite = AnimatedSprite2D.new()
var frames = SpriteFrames.new()

# Add animation
frames.add_animation("idle")
frames.set_animation_speed("idle", 8.0)
frames.set_animation_loop("idle", true)

# Add frames from individual images
frames.add_frame("idle", preload("res://assets/idle_0.png"))
frames.add_frame("idle", preload("res://assets/idle_1.png"))

# Or from sprite sheet using AtlasTexture
var sheet = preload("res://assets/sheet.png")
for i in range(4):
	var atlas = AtlasTexture.new()
	atlas.atlas = sheet
	atlas.region = Rect2(i * 32, 0, 32, 32)
	frames.add_frame("idle", atlas)

frames.remove_animation("default")
anim_sprite.sprite_frames = frames
anim_sprite.play("idle")
```

## Tween (Procedural Animation)
One-shot or chained property animations without AnimationPlayer.

```gdscript
# Simple fade out
var tween = create_tween()
tween.tween_property($Sprite2D, "modulate:a", 0.0, 0.5)

# Chain animations
var tween = create_tween()
tween.tween_property($Sprite2D, "position", Vector2(200, 0), 0.5)
tween.tween_property($Sprite2D, "rotation", PI, 0.3)
tween.tween_callback($Sprite2D.queue_free)

# Parallel animations
var tween = create_tween().set_parallel()
tween.tween_property($Sprite2D, "position:x", 200.0, 0.5)
tween.tween_property($Sprite2D, "modulate:a", 0.0, 0.5)

# Easing
tween.tween_property(node, "position", target, 0.5).set_ease(Tween.EASE_OUT).set_trans(Tween.TRANS_BOUNCE)

# Looping
var tween = create_tween().set_loops()  # infinite
tween.tween_property($Sprite2D, "position:y", -10.0, 0.5).as_relative()
tween.tween_property($Sprite2D, "position:y", 10.0, 0.5).as_relative()
```

## Timer
```gdscript
var timer = Timer.new()
timer.wait_time = 2.0
timer.one_shot = true  # or false for repeating
timer.timeout.connect(_on_timer_timeout)
add_child(timer)
timer.start()
```
