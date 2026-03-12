# Audio in Godot 4.x

## Audio Players
- `AudioStreamPlayer` — non-positional (UI sounds, background music)
- `AudioStreamPlayer2D` — 2D positional audio (attenuates with distance from AudioListener2D)
- `AudioStreamPlayer3D` — 3D positional audio

## Basic Usage
```gdscript
# Background music
var bgm = AudioStreamPlayer.new()
bgm.stream = preload("res://assets/music.ogg")
bgm.bus = "Music"
bgm.autoplay = true
add_child(bgm)

# Sound effect
var sfx = AudioStreamPlayer.new()
sfx.stream = preload("res://assets/jump.wav")
sfx.bus = "SFX"
add_child(sfx)
sfx.play()

# 2D positional
var sound = AudioStreamPlayer2D.new()
sound.stream = preload("res://assets/explosion.wav")
sound.max_distance = 500.0
add_child(sound)
sound.play()
```

## Signals
- `finished` — emitted when playback finishes (non-looping)

## Properties
- `volume_db: float` — volume in decibels (0 = normal, -80 = silent)
- `pitch_scale: float` — playback speed/pitch (1.0 = normal)
- `autoplay: bool` — start playing on ready
- `bus: String` — audio bus name ("Master", "Music", "SFX")
- `max_distance: float` — (2D/3D only) max hearing distance

## Tips
- Use `.ogg` for music (looping, compressed)
- Use `.wav` for short SFX (low latency)
- Set `stream.loop = true` for looping music in OGG files
