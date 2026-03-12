# Particle Systems in Godot 4.x

## GPUParticles2D
```gdscript
var particles = GPUParticles2D.new()
particles.amount = 32
particles.lifetime = 1.0
particles.one_shot = false  # true for burst effects (explosions)
particles.emitting = true

# Process material controls behavior
var mat = ParticleProcessMaterial.new()
mat.direction = Vector3(0, -1, 0)  # yes, Vector3 even for 2D
mat.initial_velocity_min = 50.0
mat.initial_velocity_max = 100.0
mat.gravity = Vector3(0, 98, 0)
mat.spread = 30.0  # degrees
mat.scale_min = 0.5
mat.scale_max = 1.5
mat.color = Color(1, 0.5, 0, 1)  # orange

# Color ramp (fade out)
var gradient = Gradient.new()
gradient.set_color(0, Color(1, 1, 0, 1))   # yellow start
gradient.set_color(1, Color(1, 0, 0, 0))   # red transparent end
var gradient_tex = GradientTexture1D.new()
gradient_tex.gradient = gradient
mat.color_ramp = gradient_tex

particles.process_material = mat
```

## GPUParticles3D
```gdscript
var particles = GPUParticles3D.new()
particles.amount = 64
particles.lifetime = 2.0
var mat = ParticleProcessMaterial.new()
mat.direction = Vector3(0, 1, 0)
mat.initial_velocity_min = 2.0
mat.initial_velocity_max = 5.0
mat.gravity = Vector3(0, -9.8, 0)
particles.process_material = mat
```

## Common Effects
- **Explosion**: `one_shot = true`, high `initial_velocity`, high `spread` (180), short `lifetime`
- **Fire**: upward direction, orange→red color ramp, fade alpha
- **Smoke**: slow velocity, gray color, scale up over time
- **Trail**: attach to moving object, low spread, moderate lifetime
- **Rain**: downward direction, full-screen emission, no gravity override
- **Sparkle**: random direction, short lifetime, small scale, bright color
