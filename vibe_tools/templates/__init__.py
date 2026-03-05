"""Game template registry for GodotVibe."""

from .base import GameTemplate
from .blank import BlankTemplate
from .platformer_2d import Platformer2DTemplate
from .topdown_2d import TopDown2DTemplate
from .fps_3d import FPS3DTemplate
from .tps_3d import TPS3DTemplate

TEMPLATES: dict[str, type[GameTemplate]] = {
	"blank": BlankTemplate,
	"platformer_2d": Platformer2DTemplate,
	"topdown_2d": TopDown2DTemplate,
	"fps_3d": FPS3DTemplate,
	"tps_3d": TPS3DTemplate,
}


def get_template(name: str) -> GameTemplate:
	"""Get an instantiated template by name."""
	if name not in TEMPLATES:
		raise ValueError(f"Unknown template: {name}. Available: {list(TEMPLATES.keys())}")
	return TEMPLATES[name]()
