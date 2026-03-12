# Shaders in Godot 4.x

## Shader Types
- `shader_type canvas_item;` — 2D sprites, UI
- `shader_type spatial;` — 3D surfaces
- `shader_type particles;` — GPU particle processing
- `shader_type sky;` — sky rendering
- `shader_type fog;` — volumetric fog

## Applying Shaders
```gdscript
var shader_material = ShaderMaterial.new()
shader_material.shader = preload("res://shaders/my_shader.gdshader")
shader_material.set_shader_parameter("speed", 2.0)
sprite.material = shader_material
```

## 2D Shader Examples

### Flash White (hit effect)
```glsl
shader_type canvas_item;
uniform float flash_amount : hint_range(0.0, 1.0) = 0.0;
void fragment() {
	vec4 tex = texture(TEXTURE, UV);
	COLOR = mix(tex, vec4(1.0, 1.0, 1.0, tex.a), flash_amount);
}
```

### Outline
```glsl
shader_type canvas_item;
uniform vec4 outline_color : source_color = vec4(0, 0, 0, 1);
uniform float outline_width = 1.0;
void fragment() {
	vec4 tex = texture(TEXTURE, UV);
	float a = tex.a;
	a += texture(TEXTURE, UV + vec2(outline_width * TEXTURE_PIXEL_SIZE.x, 0)).a;
	a += texture(TEXTURE, UV - vec2(outline_width * TEXTURE_PIXEL_SIZE.x, 0)).a;
	a += texture(TEXTURE, UV + vec2(0, outline_width * TEXTURE_PIXEL_SIZE.y)).a;
	a += texture(TEXTURE, UV - vec2(0, outline_width * TEXTURE_PIXEL_SIZE.y)).a;
	a = min(a, 1.0);
	COLOR = mix(outline_color * vec4(1,1,1,a), tex, tex.a);
}
```

## 3D Shader (StandardMaterial3D as alternative)
```gdscript
var mat = StandardMaterial3D.new()
mat.albedo_color = Color(0.8, 0.2, 0.2)
mat.metallic = 0.5
mat.roughness = 0.3
mat.emission_enabled = true
mat.emission = Color(1, 0, 0)
mat.emission_energy_multiplier = 2.0
mesh_instance.material_override = mat
```
