"""API Knowledge Base builder - parses Godot's doc/classes/ XML files."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from vibe_tools.models import (
	APIClass, APIMethod, APIParam, APIProperty, APISignal, APIConstant, APIEnum,
)

# Category classification rules based on class inheritance and naming
_CATEGORY_RULES: list[tuple[str, list[str]]] = [
	("2d", ["2D", "2d", "Parallax", "TileMap", "TileSet", "Tile"]),
	("3d", ["3D", "3d", "Mesh", "CSG", "GridMap", "Decal", "Fog", "Lightmap",
			"VoxelGI", "Occluder", "Sky", "Reflection", "SpringArm"]),
	("physics", ["Physics", "Body2D", "Body3D", "Joint", "Shape2D", "Shape3D",
				 "RayCast", "ShapeCast", "Area2D", "Area3D", "Collision",
				 "Soft", "Vehicle", "Kinematic"]),
	("gui", ["Control", "Button", "Label", "Container", "Slider", "ScrollBar",
			 "TextEdit", "LineEdit", "ProgressBar", "Tree", "ItemList",
			 "GraphEdit", "GraphNode", "TabBar", "TabContainer", "MenuBar",
			 "Panel", "Popup", "Dialog", "Separator", "ColorPicker",
			 "ColorRect", "TextureRect", "NinePatch", "SpinBox",
			 "RichTextLabel", "CodeEdit", "OptionButton", "CheckBox",
			 "CheckButton", "LinkButton", "MenuButton", "VideoStream",
			 "Foldable", "Range", "BaseButton", "BoxContainer",
			 "MarginContainer", "CenterContainer", "GridContainer",
			 "SplitContainer", "FlowContainer", "AspectRatio", "SubViewportContainer"]),
	("animation", ["Animation", "Tween", "Tweener"]),
	("audio", ["Audio", "Sound"]),
	("navigation", ["Navigation", "AStar"]),
	("networking", ["Multiplayer", "ENet", "WebSocket", "WebRTC", "HTTP",
					"TCP", "UDP", "StreamPeer", "PacketPeer", "DTLS", "TLS",
					"UPNP", "IP"]),
	("input", ["Input", "Shortcut", "Gesture"]),
	("rendering", ["Rendering", "Shader", "Material", "Texture", "Image",
				   "Viewport", "SubViewport", "Canvas", "VisualShader",
				   "Environment", "Camera", "Light", "RD"]),
	("xr", ["XR", "OpenXR", "WebXR"]),
	("resources", ["Resource", "Packed", "Config", "JSON", "XML", "File",
				   "Dir", "Crypto", "Zip", "PCK"]),
	("math", ["Vector", "Basis", "Transform", "Quaternion", "AABB", "Plane",
			  "Rect2", "Projection", "Color", "Curve", "Gradient", "Noise",
			  "Geometry", "Random", "Expression"]),
	("text", ["Font", "Text", "Translation"]),
	("core", ["Object", "RefCounted", "Node", "SceneTree", "Engine",
			  "ClassDB", "MainLoop", "Variant", "OS", "Time", "Display",
			  "Performance", "Thread", "Mutex", "Semaphore", "Worker",
			  "GDExtension", "Callable", "Signal", "Script"]),
]


def _classify(class_name: str, inherits: str | None) -> str:
	"""Classify a Godot class into a category."""
	for category, keywords in _CATEGORY_RULES:
		for kw in keywords:
			if kw in class_name:
				return category
	if inherits:
		for category, keywords in _CATEGORY_RULES:
			for kw in keywords:
				if kw in inherits:
					return category
	return "other"


def _parse_params(element: ET.Element) -> list[APIParam]:
	"""Parse param elements from a method/signal/constructor."""
	params = []
	for p in element.findall("param"):
		params.append(APIParam(
			name=p.get("name", ""),
			type=p.get("type", "Variant"),
			default=p.get("default"),
		))
	return params


def _get_text(element: ET.Element | None) -> str:
	"""Get text content of an element, stripped."""
	if element is None:
		return ""
	text = element.text or ""
	return text.strip()


def parse_class_xml(xml_path: str) -> APIClass:
	"""Parse a single Godot API XML documentation file."""
	tree = ET.parse(xml_path)
	root = tree.getroot()

	name = root.get("name", "")
	inherits = root.get("inherits")

	cls = APIClass(
		name=name,
		inherits=inherits,
		brief_description=_get_text(root.find("brief_description")),
		description=_get_text(root.find("description")),
		category=_classify(name, inherits),
	)

	# Parse methods
	methods_el = root.find("methods")
	if methods_el is not None:
		for m in methods_el.findall("method"):
			ret_el = m.find("return")
			ret_type = ret_el.get("type", "void") if ret_el is not None else "void"
			cls.methods.append(APIMethod(
				name=m.get("name", ""),
				return_type=ret_type,
				description=_get_text(m.find("description")),
				params=_parse_params(m),
				qualifiers=m.get("qualifiers", ""),
			))

	# Parse properties (members)
	members_el = root.find("members")
	if members_el is not None:
		for p in members_el.findall("member"):
			cls.properties.append(APIProperty(
				name=p.get("name", ""),
				type=p.get("type", "Variant"),
				default=p.get("default"),
				description=(p.text or "").strip(),
				setter=p.get("setter", ""),
				getter=p.get("getter", ""),
			))

	# Parse signals
	signals_el = root.find("signals")
	if signals_el is not None:
		for s in signals_el.findall("signal"):
			cls.signals.append(APISignal(
				name=s.get("name", ""),
				description=_get_text(s.find("description")),
				params=_parse_params(s),
			))

	# Parse constants and enums
	constants_el = root.find("constants")
	if constants_el is not None:
		enum_map: dict[str, list[APIConstant]] = {}
		for c in constants_el.findall("constant"):
			const = APIConstant(
				name=c.get("name", ""),
				value=c.get("value", ""),
				description=(c.text or "").strip(),
				enum_name=c.get("enum"),
			)
			if const.enum_name:
				enum_map.setdefault(const.enum_name, []).append(const)
			else:
				cls.constants.append(const)

		for enum_name, values in enum_map.items():
			cls.enums.append(APIEnum(name=enum_name, values=values))

	return cls


class APIKnowledgeBase:
	"""In-memory API knowledge base with category indexing and search."""

	def __init__(self):
		self.classes: dict[str, APIClass] = {}
		self.categories: dict[str, list[str]] = {}  # category -> class names

	def add_class(self, cls: APIClass):
		"""Add a class to the knowledge base."""
		self.classes[cls.name] = cls
		self.categories.setdefault(cls.category, []).append(cls.name)

	def search(self, query: str, limit: int = 20) -> list[APIClass]:
		"""Search classes by name (case-insensitive partial match)."""
		query_lower = query.lower()
		results = []

		# Exact match first
		if query in self.classes:
			results.append(self.classes[query])

		# Partial match
		for name, cls in self.classes.items():
			if cls in results:
				continue
			if query_lower in name.lower():
				results.append(cls)
			elif query_lower in cls.brief_description.lower():
				results.append(cls)
			if len(results) >= limit:
				break

		return results

	def get_by_category(self, category: str) -> list[APIClass]:
		"""Get all classes in a category."""
		names = self.categories.get(category, [])
		return [self.classes[n] for n in names if n in self.classes]

	def get_inheritance_chain(self, class_name: str) -> list[str]:
		"""Get the full inheritance chain for a class."""
		chain = []
		current = class_name
		while current and current in self.classes:
			chain.append(current)
			current = self.classes[current].inherits
		return chain

	@staticmethod
	def build_from_xml(doc_classes_dir: str) -> APIKnowledgeBase:
		"""Build knowledge base by parsing all XML files in doc/classes/."""
		kb = APIKnowledgeBase()
		doc_path = Path(doc_classes_dir)

		for xml_file in sorted(doc_path.glob("*.xml")):
			try:
				cls = parse_class_xml(str(xml_file))
				kb.add_class(cls)
			except Exception as e:
				print(f"  Warning: Failed to parse {xml_file.name}: {e}")

		return kb

	def save_to_cache(self, cache_path: str):
		"""Save knowledge base to JSON cache file."""
		data = {
			"version": "1.0",
			"class_count": len(self.classes),
			"categories": {k: v for k, v in sorted(self.categories.items())},
			"classes": {},
		}

		for name, cls in sorted(self.classes.items()):
			data["classes"][name] = {
				"name": cls.name,
				"inherits": cls.inherits,
				"brief": cls.brief_description,
				"category": cls.category,
				"methods": [
					{
						"name": m.name,
						"return": m.return_type,
						"params": [{"name": p.name, "type": p.type, "default": p.default} for p in m.params],
						"qualifiers": m.qualifiers,
					}
					for m in cls.methods
				],
				"properties": [
					{"name": p.name, "type": p.type, "default": p.default}
					for p in cls.properties
				],
				"signals": [
					{"name": s.name, "params": [{"name": p.name, "type": p.type} for p in s.params]}
					for s in cls.signals
				],
				"enums": [
					{"name": e.name, "values": [{"name": v.name, "value": v.value} for v in e.values]}
					for e in cls.enums
				],
			}

		os.makedirs(os.path.dirname(cache_path), exist_ok=True)
		with open(cache_path, "w", encoding="utf-8") as f:
			json.dump(data, f, indent=2, ensure_ascii=False)

	@staticmethod
	def load_from_cache(cache_path: str) -> APIKnowledgeBase:
		"""Load knowledge base from JSON cache file."""
		with open(cache_path, "r", encoding="utf-8") as f:
			data = json.load(f)

		kb = APIKnowledgeBase()
		for name, cd in data["classes"].items():
			cls = APIClass(
				name=cd["name"],
				inherits=cd.get("inherits"),
				brief_description=cd.get("brief", ""),
				category=cd.get("category", "other"),
			)
			for m in cd.get("methods", []):
				cls.methods.append(APIMethod(
					name=m["name"],
					return_type=m.get("return", "void"),
					params=[APIParam(name=p["name"], type=p["type"], default=p.get("default")) for p in m.get("params", [])],
					qualifiers=m.get("qualifiers", ""),
				))
			for p in cd.get("properties", []):
				cls.properties.append(APIProperty(
					name=p["name"],
					type=p["type"],
					default=p.get("default"),
				))
			for s in cd.get("signals", []):
				cls.signals.append(APISignal(
					name=s["name"],
					params=[APIParam(name=p["name"], type=p["type"]) for p in s.get("params", [])],
				))
			for e in cd.get("enums", []):
				cls.enums.append(APIEnum(
					name=e["name"],
					values=[APIConstant(name=v["name"], value=v["value"]) for v in e.get("values", [])],
				))
			kb.add_class(cls)

		return kb
