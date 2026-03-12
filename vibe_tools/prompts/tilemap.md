# TileMap / TileMapLayer in Godot 4.x

**IMPORTANT**: Godot 4.x uses `TileMapLayer` (NOT `TileMap` which is deprecated).

## Basic Setup
```gdscript
var tilemap = TileMapLayer.new()
tilemap.tile_set = TileSet.new()

# Configure tile size
tilemap.tile_set.tile_size = Vector2i(16, 16)

# Add a source (atlas from texture)
var source = TileSetAtlasSource.new()
source.texture = preload("res://assets/tileset.png")
source.texture_region_size = Vector2i(16, 16)
tilemap.tile_set.add_source(source)
```

## Placing Tiles in Code
```gdscript
# set_cell(coords, source_id, atlas_coords, alternative_tile)
tilemap.set_cell(Vector2i(5, 3), 0, Vector2i(0, 0))  # place tile at grid (5,3)
tilemap.set_cell(Vector2i(5, 4), 0, Vector2i(1, 0))  # different tile from atlas

# Erase a cell
tilemap.erase_cell(Vector2i(5, 3))
```

## TileSet with Physics
```gdscript
# Add physics layer to TileSet
tilemap.tile_set.add_physics_layer()
tilemap.tile_set.set_physics_layer_collision_layer(0, 1)
# Individual tiles need physics polygons set in the TileSet resource
```

## Programmatic Level Generation
```gdscript
# Fill a floor
for x in range(-10, 10):
	tilemap.set_cell(Vector2i(x, 5), 0, Vector2i(0, 0))

# Build walls
for y in range(-5, 6):
	tilemap.set_cell(Vector2i(-10, y), 0, Vector2i(1, 0))
	tilemap.set_cell(Vector2i(9, y), 0, Vector2i(1, 0))
```

## Tips
- Use `.tres` TileSet resources created with `create_resource` tool for complex tilesets
- Each TileMapLayer is ONE layer — use multiple TileMapLayer nodes for foreground/background
- Coordinates are in grid space (integer), convert with `tilemap.map_to_local(grid_pos)` and `tilemap.local_to_map(world_pos)`
