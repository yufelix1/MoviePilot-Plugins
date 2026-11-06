"""MoviePilot V2 entry point for stateless CMS-triggered 115 organization."""

from __future__ import annotations

from datetime import datetime
from threading import Event
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase

from .core import DEFAULTS, Options, time_window
from .service import OrganizerService
from .ui import build_form


class CMS115Organizer(_PluginBase):
    """Register host lifecycle, public service and the Vuetify configuration form."""

    plugin_name = "CMS115自动整理"
    plugin_desc = "基于 CMS 同步事件，自动识别并整理 115 网盘。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/u115.png"
    plugin_version = "1.0.1"
    plugin_author = "yufelix"
    author_url = "https://github.com/yufelix1"
    plugin_config_prefix = "cms115organizer_"
    plugin_order = 100
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._stopped = Event()
        self._config = dict(DEFAULTS)
        self._options = None

    def init_plugin(self, config: dict = None):
        """Cancel the old execution before applying the host-provided configuration."""
        self.stop_service()
        self._config = {key: (config or {}).get(key, value) for key, value in DEFAULTS.items()}
        self._options = None
        if self._config["enabled"]:
            try:
                self._options = Options.from_config(self._config)
            except (ValueError, TypeError, OSError) as error:
                logger.error(f"配置无效：{error}")
        # Old callbacks must retain their cancelled event after reinitialization.
        self._stopped = Event()

    def get_state(self) -> bool:
        """Expose enabled and valid state to the V2 host."""
        return bool(self._config["enabled"] and self._options and not self._stopped.is_set())

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """No remote commands are registered."""
        return []

    def get_api(self) -> list[dict[str, Any]]:
        """No additional plugin API is required."""
        return []

    def get_service(self) -> list[dict[str, Any]]:
        """Let the host schedule a fixed, clone-specific half-hour service."""
        if not self.get_state():
            return []
        return [{
            "id": f"{self.__class__.__name__}.Organize",
            "name": self.plugin_name,
            "trigger": CronTrigger.from_crontab("*/30 * * * *", timezone=settings.TZ),
            "func": self.run_once,
            "kwargs": {},
        }]

    def run_once(self):
        """Run one window in the host timezone, without saving runtime data."""
        # Keep this execution bound to its configuration and cancellation event.
        options, stopped = self._options, self._stopped
        if not self._config["enabled"] or options is None or stopped.is_set():
            return
        try:
            now = datetime.now(ZoneInfo(settings.TZ)).replace(tzinfo=None)
            start, end = time_window(now)
            logger.info(f"开始自动整理任务，处理窗口 [{start}, {end})")
            report = OrganizerService(options, stopped.is_set).run(
                now=now, on_record=self._log_record,
            )
            counts = report["counts"]
            logger.info(
                f"窗口 [{report['start']}, {report['end']}) 事件处理："
                f"成功 {counts['success']} 条，失败 {counts['failed']} 条，跳过 {counts['skipped']} 条"
            )
        except Exception as error:
            logger.error(f"本轮失败，不保存或重试：{error}")

    def _log_record(self, row: dict):
        """Log readable names; the host logger already identifies the plugin."""
        status = {"success": "成功", "failed": "失败", "skipped": "跳过"}[row["status"]]
        file_name = row.get("file_name") or (f"未提供名称，ID={row['file_id']}" if row["file_id"] else "未提供名称")
        message = f"[{status}] {file_name}：{row['message']}（事件 {row['id']}）"
        if row["status"] == "failed":
            logger.warning(message)
        else:
            logger.info(message)

    def get_form(self) -> tuple[list[dict], dict]:
        """Return the standard Vuetify configuration page and default model."""
        return build_form()

    def get_page(self) -> None:
        """Keep empty so the host does not advertise a plugin data page."""
        pass

    def stop_service(self):
        """Signal active work to stop; the host owns the public-service scheduler."""
        self._stopped.set()
