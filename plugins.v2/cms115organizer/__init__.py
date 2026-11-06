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

from .core import DEFAULTS, Options
from .service import OrganizerService
from .ui import build_form, build_page


class CMS115Organizer(_PluginBase):
    """Register host lifecycle, public service and default Vuetify pages."""

    plugin_name = "CMS115自动整理"
    plugin_desc = "基于 CMS 同步事件，自动识别并整理 115 网盘。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/u115.png"
    plugin_version = "1.0.0"
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
        self._error = ""

    def init_plugin(self, config: dict = None):
        """Cancel the old execution before applying the host-provided configuration."""
        self.stop_service()
        self._config = {key: (config or {}).get(key, value) for key, value in DEFAULTS.items()}
        self._options, self._error = None, ""
        if self._config["enabled"]:
            try:
                self._options = Options.from_config(self._config)
            except (ValueError, TypeError, OSError) as error:
                self._error = str(error)
                logger.error(f"【{self.plugin_name}】配置无效：{error}")
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
            report = OrganizerService(options, stopped.is_set).run(
                now=now, on_record=self._log_record,
            )
            logger.info(f"【{self.plugin_name}】窗口 [{report['start']}, {report['end']})：{report['counts']}")
        except Exception as error:
            logger.error(f"【{self.plugin_name}】本轮失败，不保存或重试：{error}")

    def _log_record(self, row: dict):
        """Use the runtime display name so clone logs can be distinguished."""
        message = f"【{self.plugin_name}】事件 {row['id']} [{row['status']}] 对象 {row['file_id']}：{row['message']}"
        if row["status"] == "failed":
            logger.warning(message)
        else:
            logger.info(message)

    def get_form(self) -> tuple[list[dict], dict]:
        """Return the standard Vuetify configuration page and default model."""
        return build_form()

    def get_page(self) -> list[dict]:
        """Render static help and configuration errors without accessing plugin data."""
        return build_page(self._error)

    def stop_service(self):
        """Signal active work to stop; the host owns the public-service scheduler."""
        self._stopped.set()
