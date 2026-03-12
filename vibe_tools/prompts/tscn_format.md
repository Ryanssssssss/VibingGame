# Godot .tscn Scene File Format (4.x, format=3)

## Structure Order
A `.tscn` file MUST follow this exact section order:
1. `[gd_scene]` — header with load_steps and format=3
2. `[ext_resource]` — external resources (scripts, textures, audio, fonts, etc.)
3. `[sub_resource]` — inline sub-resources (shapes, materials, animations, etc.)
4. `[node]` — node tree
5. `[connection]` — signal connections

## Header
```
[gd_scene load_steps=N format=3 uid="uid://xxxxx"]
```

## External Resources
```
[ext_resource type="Script" path="res://player.gd" id="1_abc"]
[ext_resource type="Texture2D" path="res://assets/player.png" id="2_def"]
```
- ID format: `数字_随机字符` (e.g., `1_abc`, `2_xyz`)

## Sub Resources
```
[sub_resource type="RectangleShape2D" id="SubResource(\"1_ghi\")"]
size = Vector2(16, 16)

[sub_resource type="CircleShape2D" id="SubResource(\"2_jkl\")"]
radius = 8.0
```

## Node Tree
```
[node name="Root" type="Node2D"]

[node name="Player" type="CharacterBody2D" parent="."]
script = ExtResource("1_abc")

[node name="Sprite2D" type="Sprite2D" parent="Player"]
texture = ExtResource("2_def")

[node name="CollisionShape2D" type="CollisionShape2D" parent="Player"]
shape = SubResource("1_ghi")
```
- Root node: NO `parent` field
- Direct children of root: `parent="."`
- Deeper children: `parent="Player"`, `parent="Player/Sprite2D"`, etc.

## Signal Connections
```
[connection signal="body_entered" from="Player/Area2D" to="Player" method="_on_area_2d_body_entered"]
```

## Common Pitfalls
1. `CollisionShape2D`/`CollisionShape3D` MUST have a `shape` SubResource — crashes without it
2. `load_steps` must match the total count of ext_resource + sub_resource + 1
3. Node names cannot contain `/` — use underscores instead
4. Resource IDs must be unique within the file
