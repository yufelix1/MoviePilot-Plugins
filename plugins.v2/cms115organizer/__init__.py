from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from threading import Event, Lock
from time import monotonic
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import FileItem


# 每天 8–23 点的 25、55 分运行，单轮最多执行 20 分钟。
RUN_TIMEOUT_SECONDS = 20 * 60
DEFAULT_CRON = "25,55 8-23 * * *"

DEFAULTS = {
    "enabled": False,
    "db_path": "",
    "source_directory_id": "",
}


class CMS115Organizer(_PluginBase):
    plugin_name = "CMS115自动整理"
    plugin_desc = "基于 CMS 同步事件，自动识别并整理 115 网盘。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/u115.png"
    plugin_version = "1.0.5"
    plugin_author = "yufelix"
    author_url = "https://github.com/yufelix1"
    plugin_config_prefix = "cms115organizer_"
    plugin_order = 100
    auth_level = 1

    def __init__(self):
        super().__init__()
        # 重载配置时保留执行锁，避免旧任务尚未结束时重复执行。
        self._state_lock = Lock()
        self._run_lock = Lock()
        self._stopped = Event()
        self._config = dict(DEFAULTS)
        self._options: Options | None = None

    def init_plugin(self, config: dict = None):
        """停止旧任务并读取配置，旧回调继续持有已取消的退出事件。"""
        with self._state_lock:
            self._stopped.set()
            self._config = {
                key: (config or {}).get(key, value) for key, value in DEFAULTS.items()
            }
            self._options = None
            if self._config["enabled"]:
                try:
                    options = Options.from_config(self._config)
                    # 启动前检查可读性与表结构，不读取事件或访问网盘。
                    with CmsDatabase(options.db_path):
                        pass
                    self._options = options
                except (ValueError, TypeError, OSError, sqlite3.DatabaseError) as error:
                    logger.error(f"配置无效：{error}")
            self._stopped = Event()

    def run_once(self):
        """执行一轮整理，跳过尚有任务运行时的重复调用。"""
        with self._state_lock:
            if self._options is None or self._stopped.is_set():
                return
            options, stopped = self._options, self._stopped
        if not self._run_lock.acquire(blocking=False):
            logger.warning("上一轮整理尚未结束，跳过本次执行")
            return
        try:
            if not stopped.is_set():
                self._run_window(options, stopped)
        finally:
            self._run_lock.release()

    def _run_window(self, options: Options, stopped: Event):
        """按宿主时区处理一个窗口，配置与退出事件在本轮内保持固定。"""
        deadline = monotonic() + RUN_TIMEOUT_SECONDS
        timed_out = False

        def should_stop() -> bool:
            nonlocal timed_out
            if timed_out or stopped.is_set():
                return True
            if monotonic() >= deadline:
                timed_out = True
                logger.warning(
                    "任务超时时间20 分钟，已超时，停止后续整理；正在执行的请求需等待宿主返回，不保存或重试"
                )
                return True
            return False

        try:
            now = datetime.now(ZoneInfo(settings.TZ)).replace(tzinfo=None)
            start, end = time_window(now)
            logger.info(f"开始自动整理任务，处理窗口 [{start}, {end})")
            counts = self._process_window(
                options,
                should_stop,
                start,
                end,
                on_record=self._log_record,
            )
            logger.info(
                f"窗口 [{start}, {end}) 事件处理："
                f"成功 {counts['success']} 条，失败 {counts['failed']} 条，跳过 {counts['skipped']} 条"
            )
        except Exception as error:
            logger.error(f"本轮失败，不保存或重试：{error}")

    def _process_window(
        self,
        options: Options,
        stopped: Callable[[], bool],
        start: datetime,
        end: datetime,
        on_record: Callable[[dict], None],
    ) -> dict:
        """处理单轮事件，目录查询结果或异常只在本轮内使用。"""
        items = None
        lookup_error = ""

        def transfer(record: dict) -> tuple[bool, str]:
            nonlocal items, lookup_error
            if stopped():
                return False, "任务已停止或超时，本次丢弃"
            # 首次匹配到待整理事件时才查询网盘，查询结果或异常本轮共用。
            if items is None:
                items = {}
                try:
                    items = self._get_source_items(options, stopped)
                except Exception as error:
                    lookup_error = "待整理目录不可用，本次丢弃；请查看本轮目录查询错误日志"
                    logger.error(
                        f"查询待整理目录失败（ID={options.source_directory_id}）："
                        f"{type(error).__name__}: {error}"
                    )
            if stopped():
                return False, "任务已停止或超时，本次丢弃"
            if lookup_error:
                return False, lookup_error
            fileitem = items.get(record["file_id"])
            if not fileitem:
                return False, "待整理目录内未找到对应文件或目录，本次丢弃"
            # 宿主可能移动或重命名 FileItem，提交前保留原始名称。
            record["file_name"] = (
                getattr(fileitem, "name", None) or PurePosixPath(fileitem.path).name
            )
            return self._transfer_item(fileitem, stopped)

        events = []
        if not stopped():
            # 先读取事件快照并关闭数据库，再发起可能耗时的网盘查询和整理。
            with CmsDatabase(options.db_path) as database:
                events = database.events_in_window(start, end)
        return self._process_events(events, options, transfer, stopped, on_record)

    @staticmethod
    def _process_events(
        events: list[dict],
        options: Options,
        transfer: Callable[[dict], tuple[bool, str]],
        stopped: Callable[[], bool],
        on_record: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """按时间倒序处理事件，仅提交直接子项，每轮去重一次。"""
        counts = {status: 0 for status in ("success", "failed", "skipped")}
        seen = set()
        for event in events:
            if stopped():
                break
            record = {
                "id": event["id"],
                "event": event["behavior_type"],
                "file_id": "",
                "file_name": "",
            }
            try:
                item = json.loads(event["item_json"])
                if not isinstance(item, dict):
                    raise ValueError("item_json 必须是单个文件或目录的 JSON 对象")
                # 跳过事件或整理异常时也保留名称与 ID。
                file_id = record["file_id"] = str(item.get("file_id") or "")
                if isinstance(item.get("file_name"), str):
                    record["file_name"] = item["file_name"]
                if str(item.get("parent_id", "")) != options.source_directory_id:
                    status, message = "skipped", "父目录 ID 未匹配待整理目录（仅匹配直接子项）"
                elif not file_id.isascii() or not file_id.isdigit() or file_id == "0":
                    raise ValueError("事件缺少有效的 file_id")
                elif file_id in seen:
                    status, message = "skipped", "本轮已处理该文件或目录，不重复提交或重试"
                else:
                    # 整理失败也不在本轮重试；无效 ID 不参与去重。
                    seen.add(file_id)
                    success, message = transfer(record)
                    status = "success" if success else "failed"
                    message = (
                        "整理完成" if success
                        else str(message or "MoviePilot 整理失败，本次丢弃")
                    )
            except Exception as error:
                status, message = "failed", f"本次丢弃：{error}"
            counts[status] += 1
            if on_record:
                on_record({**record, "status": status, "message": message})
        return counts

    @staticmethod
    def _transfer_item(
        fileitem: FileItem, stopped: Callable[[], bool]
    ) -> tuple[bool, str]:
        """同步提交宿主整理，识别、目标目录与递归处理均使用宿主配置。"""
        if stopped():
            return False, "任务已停止或超时，本次丢弃"
        success, message = TransferChain().do_transfer(
            fileitem=fileitem,
            background=False,
            force=False,
            continue_callback=lambda: not stopped(),
        )
        if stopped():
            return False, "任务停止或超时时整理可能尚未完成，不保存或重试"
        return bool(success), str(message or "")

    @staticmethod
    def _get_source_items(
        options: Options, stopped: Callable[[], bool] = lambda: False
    ) -> dict[str, FileItem]:
        """匹配待整理目录 ID，每轮只查询一次直接子项。"""
        chain = StorageChain()
        checked, matches, errors = set(), [], []
        for config in DirectoryHelper().get_download_dirs():
            if stopped():
                return {}
            if config.storage not in {"u115", "115网盘Plus"} or not config.monitor_type:
                continue
            try:
                path = _cloud_path(config.download_path)
                key = (config.storage, path)
                # 不同媒体分类可能共用同一个资源目录。
                if key in checked:
                    continue
                checked.add(key)
                directory = chain.get_file_item(storage=config.storage, path=Path(path))
                if (
                    not directory
                    or str(directory.fileid) != options.source_directory_id
                ):
                    continue
                if directory.type != "dir" or _cloud_path(directory.path) != path:
                    raise ValueError("宿主资源目录类型或路径不一致")
                matches.append((config.storage, path, directory))
            except Exception as error:
                errors.append(
                    f"{config.storage} 路径 {config.download_path!r}："
                    f"{type(error).__name__}: {error}"
                )
        if not matches:
            detail = f"：{'；'.join(errors)}" if errors else ""
            raise ValueError("未找到待整理目录 ID 对应的已启用 115 资源目录" + detail)
        if len(matches) != 1:
            paths = "；".join(
                f"{storage} {path}" for storage, path, _directory in matches
            )
            raise ValueError(f"待整理目录来源不唯一：{paths}")
        _storage, path, directory = matches[0]
        if stopped():
            return {}
        items = chain.list_files(directory, recursion=False)
        if items is None:
            raise ValueError("网盘待整理目录查询失败，本轮丢弃")
        result = {}
        for item in items:
            if stopped():
                break
            try:
                if str(item.parent_fileid) != options.source_directory_id:
                    continue
                if str(PurePosixPath(_cloud_path(item.path)).parent) != path:
                    continue
                result[str(item.fileid)] = item
            except (AttributeError, TypeError, ValueError) as error:
                name = (
                    getattr(item, "name", None)
                    or getattr(item, "path", None)
                    or "未提供名称"
                )
                logger.warning(
                    f"跳过异常网盘子项 {name!r}（ID={getattr(item, 'fileid', '')}）：{error}"
                )
        return result

    def _log_record(self, row: dict):
        """记录整理结果，优先展示网盘中的原始名称。"""
        status = {"success": "成功", "failed": "失败", "skipped": "跳过"}[row["status"]]
        file_name = row.get("file_name") or (
            f"未提供名称，ID={row['file_id']}" if row["file_id"] else "未提供名称"
        )
        message = f"[{status}] {file_name}：{row['message']}（事件 {row['id']}）"
        if row["status"] == "failed":
            logger.warning(message)
        else:
            logger.info(message)

    def get_state(self) -> bool:
        """返回插件是否启用且配置有效。"""
        with self._state_lock:
            return self._options is not None and not self._stopped.is_set()

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """注册远程命令。"""
        return []

    def get_api(self) -> list[dict[str, Any]]:
        """注册插件 API。"""
        return []

    def get_service(self) -> list[dict[str, Any]]:
        """注册自动整理定时服务。"""
        if not self.get_state():
            return []
        return [
            {
                "id": "CMS115Organizer.Organize",
                "name": self.plugin_name,
                "trigger": CronTrigger.from_crontab(DEFAULT_CRON, timezone=settings.TZ),
                "func": self.run_once,
                "kwargs": {},
            }
        ]

    def get_form(self) -> tuple[list[dict], dict]:
        """拼装配置页面，返回页面结构和默认配置。"""

        def column(component: str, props: dict, md: int = 12) -> dict:
            return {
                "component": "VCol",
                "props": {"cols": 12, "md": md},
                "content": [{"component": component, "props": props}],
            }

        rows = [
            [column("VSwitch", {"model": "enabled", "label": "启用插件"})],
            [
                column(
                    "VTextField",
                    {
                        "model": "db_path",
                        "label": "CMS 数据库路径",
                        "placeholder": "/cms/config/cms-online.db",
                    },
                ),
                column(
                    "VTextField",
                    {
                        "model": "source_directory_id",
                        "label": "待整理目录 ID",
                    },
                ),
            ],
        ]
        return [
            {
                "component": "VForm",
                "content": [{"component": "VRow", "content": row} for row in rows],
            }
        ], dict(DEFAULTS)

    def get_page(self) -> None:
        """插件不提供数据页面。"""
        return None

    def stop_service(self):
        """通知当前任务停止，公共服务的调度器由宿主管理。"""
        with self._state_lock:
            self._stopped.set()


def time_window(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """返回截至最近一次 :25/:55 调度点的半小时窗口 [start, end)。"""
    now = now if now is not None else datetime.now()
    elapsed_minutes = (now.minute - 25) % 30
    end = now.replace(second=0, microsecond=0) - timedelta(minutes=elapsed_minutes)
    return end - timedelta(minutes=30), end


@dataclass(frozen=True)
class Options:
    """经过校验的插件配置。"""

    db_path: Path
    source_directory_id: str

    @classmethod
    def from_config(cls, config: dict) -> "Options":
        """校验已存在的数据库路径和待整理目录 ID。"""
        raw_path = str(config.get("db_path") or "").strip()
        db_path = Path(raw_path).expanduser()
        if not db_path.is_absolute() or not db_path.is_file():
            raise ValueError(
                f"CMS 数据库路径无效，须为已存在文件的绝对路径：{raw_path!r}"
            )
        directory_id = str(config.get("source_directory_id") or "").strip()
        if not directory_id.isascii() or not directory_id.isdigit():
            raise ValueError(f"待整理目录 ID 须为数字：{directory_id!r}")
        return cls(db_path=db_path.resolve(), source_directory_id=directory_id)


class CmsDatabase:
    """使用只读 SQLite 连接，兼容 CMS 正在写入的 WAL 数据。"""

    def __init__(self, path: Path):
        self.path = path
        self.connection: Optional[sqlite3.Connection] = None

    def __enter__(self) -> "CmsDatabase":
        try:
            self.connection = sqlite3.connect(
                self.path.as_uri() + "?mode=ro", uri=True, timeout=5
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only = ON")
            columns = {
                row[1]
                for row in self.connection.execute("PRAGMA table_info(lift_list)")
            }
            missing = {"id", "behavior_type", "item_json", "create_time"} - columns
            if missing:
                raise ValueError(
                    f"CMS 数据库 {self.path} 的 lift_list 缺少字段：{', '.join(sorted(missing))}"
                )
        except Exception as error:
            # __enter__ 失败时不会调用 __exit__，需主动释放已打开的连接。
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            if isinstance(error, sqlite3.DatabaseError):
                raise type(error)(
                    f"无法读取 CMS 数据库 {self.path}：{error}"
                ) from error
            raise
        return self

    def __exit__(self, *_args) -> None:
        self.connection.close()
        self.connection = None

    def events_in_window(self, start: datetime, end: datetime) -> list[dict]:
        """按时间倒序读取 [start, end)，在整理前关闭查询游标。"""
        with closing(
            self.connection.execute(
                "SELECT id, behavior_type, item_json FROM lift_list "
                "WHERE create_time >= ? AND create_time < ? ORDER BY create_time DESC, id DESC",
                (start.isoformat(sep=" "), end.isoformat(sep=" ")),
            )
        ) as rows:
            return [dict(row) for row in rows]


def _cloud_path(value: str) -> str:
    """校验宿主返回的绝对网盘路径。"""
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or "\x00" in value
        or "\\" in value
        or ".." in value.split("/")
    ):
        raise ValueError(f"宿主网盘路径无效：{value!r}")
    return str(PurePosixPath(value))
