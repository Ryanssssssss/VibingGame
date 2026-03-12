# GUI / HUD Controls in Godot 4.x

## Layout Containers
- `VBoxContainer` — vertical layout
- `HBoxContainer` — horizontal layout
- `GridContainer` — grid layout (set `columns` property)
- `MarginContainer` — adds margins around children
- `CenterContainer` — centers its child
- `PanelContainer` — panel background + child
- `ScrollContainer` — scrollable area
- `TabContainer` — tabbed pages
- `HSplitContainer`/`VSplitContainer` — resizable split

## Common Controls
- `Label` — text display
- `RichTextLabel` — BBCode formatted text (`[b]bold[/b]`, `[color=red]text[/color]`)
- `Button` — clickable button (signal: `pressed`)
- `TextureButton` — image-based button
- `LineEdit` — single-line text input (signal: `text_submitted`)
- `TextEdit` — multi-line text editor
- `ProgressBar` — progress display (`value`, `min_value`, `max_value`)
- `TextureRect` — image display (set `texture`, `stretch_mode`)
- `ColorRect` — solid color rectangle
- `Panel` — styled panel background
- `CheckBox` — toggle (signal: `toggled`)
- `OptionButton` — dropdown (signal: `item_selected`)
- `SpinBox` — numeric input with arrows
- `HSlider`/`VSlider` — slider controls (signal: `value_changed`)
- `ItemList` — selectable list
- `Tree` — hierarchical tree view

## Anchoring & Sizing
```gdscript
# Full screen overlay
control.set_anchors_preset(Control.PRESET_FULL_RECT)

# Top-left corner
control.set_anchors_preset(Control.PRESET_TOP_LEFT)

# Bottom-center
control.set_anchors_preset(Control.PRESET_CENTER_BOTTOM)

# Custom size
control.custom_minimum_size = Vector2(200, 50)
```

## Theme Overrides (in GDScript)
```gdscript
label.add_theme_font_size_override("font_size", 24)
label.add_theme_color_override("font_color", Color.WHITE)
label.add_theme_font_override("font", preload("res://assets/font.ttf"))
panel.add_theme_stylebox_override("panel", stylebox)
```

## HUD Pattern
```gdscript
# Create a CanvasLayer-based HUD
var hud = CanvasLayer.new()
hud.layer = 10  # above game

var container = VBoxContainer.new()
container.set_anchors_preset(Control.PRESET_TOP_LEFT)
container.offset_left = 10
container.offset_top = 10

var score_label = Label.new()
score_label.text = "Score: 0"
score_label.add_theme_font_size_override("font_size", 24)

container.add_child(score_label)
hud.add_child(container)
add_child(hud)
```
