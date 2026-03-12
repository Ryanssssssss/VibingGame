# Sprite Sheet Handling

When a user uploads a sprite sheet (a single image with multiple animation frames in a grid):

## Approach A: Sprite2D + hframes/vframes (simple uniform grid)
```gdscript
var sprite = Sprite2D.new()
sprite.texture = preload("res://assets/spritesheet.png")
sprite.hframes = 6   # columns in the grid
sprite.vframes = 4   # rows in the grid
sprite.frame = 0     # current frame index (row-major: row * hframes + col)
# Animate by changing sprite.frame in _process()
```

## Approach B: AnimatedSprite2D + SpriteFrames + AtlasTexture (multiple named animations from one sheet)
```gdscript
# Build SpriteFrames from a sprite sheet:
var sheet = preload("res://assets/spritesheet.png")
var frames = SpriteFrames.new()
var frame_w = 32  # single frame width in pixels
var frame_h = 32  # single frame height in pixels
var cols = sheet.get_width() / frame_w

# Create "idle" animation from row 0
frames.add_animation("idle")
frames.set_animation_speed("idle", 8.0)
frames.set_animation_loop("idle", true)
for i in range(4):  # 4 frames in idle row
	var atlas = AtlasTexture.new()
	atlas.atlas = sheet
	atlas.region = Rect2(i * frame_w, 0, frame_w, frame_h)
	frames.add_frame("idle", atlas)

# Create "run" animation from row 1
frames.add_animation("run")
frames.set_animation_speed("run", 10.0)
frames.set_animation_loop("run", true)
for i in range(6):  # 6 frames in run row
	var atlas = AtlasTexture.new()
	atlas.atlas = sheet
	atlas.region = Rect2(i * frame_w, 1 * frame_h, frame_w, frame_h)
	frames.add_frame("run", atlas)

# Remove default animation and assign
frames.remove_animation("default")
var anim_sprite = AnimatedSprite2D.new()
anim_sprite.sprite_frames = frames
anim_sprite.play("idle")
```

## RULES:
1. **NEVER use a sprite sheet as a single texture** — it will display the ENTIRE sheet as one image!
2. If asset metadata says "SPRITE SHEET (WxH, grid CxR, frame FxF)" — you MUST use hframes/vframes or AtlasTexture.
3. When the image has multiple animation rows, use AnimatedSprite2D + SpriteFrames + AtlasTexture per frame.
4. If you don't know the exact frame size, estimate from the image dimensions and visible grid pattern.
5. Common sprite sheet layouts: each ROW = one animation (idle, run, jump, attack, etc.)
