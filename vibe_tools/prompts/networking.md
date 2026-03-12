# Multiplayer / Networking in Godot 4.x

## High-Level Multiplayer API
```gdscript
# Server
var peer = ENetMultiplayerPeer.new()
peer.create_server(7000, 4)  # port, max_clients
multiplayer.multiplayer_peer = peer

# Client
var peer = ENetMultiplayerPeer.new()
peer.create_client("127.0.0.1", 7000)
multiplayer.multiplayer_peer = peer
```

## Signals
```gdscript
multiplayer.peer_connected.connect(_on_peer_connected)
multiplayer.peer_disconnected.connect(_on_peer_disconnected)
multiplayer.connected_to_server.connect(_on_connected)
multiplayer.connection_failed.connect(_on_failed)
multiplayer.server_disconnected.connect(_on_server_disconnected)
```

## RPC (Remote Procedure Calls)
```gdscript
# Define with annotation
@rpc("any_peer", "call_local", "reliable")
func take_damage(amount: int):
	health -= amount

# Call remotely
take_damage.rpc(10)           # call on ALL peers
take_damage.rpc_id(1, 10)    # call on server only (id=1)
```

## MultiplayerSpawner
Auto-spawn nodes across peers:
```gdscript
var spawner = MultiplayerSpawner.new()
spawner.spawn_path = NodePath("../Players")
spawner.add_spawnable_scene("res://player.tscn")
add_child(spawner)
```

## MultiplayerSynchronizer
Auto-sync properties:
```gdscript
var sync = MultiplayerSynchronizer.new()
# Configure replication in .tscn or code
# Syncs specified properties from authority to other peers
```

## Authority
```gdscript
# Check if this peer owns the node
if is_multiplayer_authority():
	# Process input only for our player
	pass

# Set authority
node.set_multiplayer_authority(peer_id)
```

## WebSocket Alternative
```gdscript
var peer = WebSocketMultiplayerPeer.new()
peer.create_server(8080)  # or create_client("ws://...")
multiplayer.multiplayer_peer = peer
```
