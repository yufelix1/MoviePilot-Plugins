"""Vuetify JSON pages rendered by the MoviePilot V2 frontend."""

from .core import DEFAULTS


def _field(component: str, model: str, label: str, **props) -> dict:
    return {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
        {"component": component, "props": {"model": model, "label": label, **props}},
    ]}


def build_form() -> tuple[list[dict], dict]:
    """Build a Vuetify form and an independent default configuration model."""
    fields = [
        _field("VSwitch", "enabled", "启用自动整理"),
        _field("VTextField", "db_path", "CMS 数据库绝对路径", placeholder="/cms/config/cms-online.db"),
        _field("VTextField", "source_directory_id", "待整理目录 ID", placeholder="115 网盘目录的数字 ID"),
    ]
    return [{"component": "VForm", "content": [
        {"component": "VRow", "content": fields},
    ]}], dict(DEFAULTS)


def build_page(error: str = "") -> list[dict]:
    """Show static usage information and the current configuration error only."""
    content = [{"component": "VAlert", "props": {"type": "info", "text": (
        "固定每半小时运行，不保存事件进度、重试队列或运行报告。请在 MoviePilot 日志中查看结果。"
    )}}]
    if error:
        content.append({"component": "VAlert", "props": {"type": "error", "text": error}})
    return content
