"""Vuetify JSON configuration form rendered by the MoviePilot V2 frontend."""

from .core import DEFAULTS


def _field(component: str, model: str, label: str, *, md: int = 6, **props) -> dict:
    return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": [
        {"component": component, "props": {"model": model, "label": label, **props}},
    ]}


def build_form() -> tuple[list[dict], dict]:
    """Build a Vuetify form and an independent default configuration model."""
    fields = [
        _field("VSwitch", "enabled", "启用自动整理", md=12),
        _field("VTextField", "db_path", "CMS 数据库路径", placeholder="/cms/config/cms-online.db"),
        _field("VTextField", "source_directory_id", "待整理目录 ID"),
    ]
    return [{"component": "VForm", "content": [
        {"component": "VRow", "content": fields},
    ]}], dict(DEFAULTS)
