"""Read one CMS time window without writing CMS or persisting processing state."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional


DEFAULTS = {
    "enabled": False,
    "db_path": "",
    "source_directory_id": "",
}


def time_window(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Return the last completed local half-hour, independent of scheduler delay."""
    now = now if now is not None else datetime.now()
    end = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
    return end - timedelta(minutes=30), end


@dataclass(frozen=True)
class Options:
    """Configuration only; no checkpoints, retry queue or configurable interval."""

    db_path: Path
    source_directory_id: str

    @classmethod
    def from_config(cls, config: dict) -> "Options":
        """Validate the existing database and the direct parent directory ID."""
        values = {**DEFAULTS, **config}
        db_path = Path(str(values["db_path"] or "").strip()).expanduser()
        if not str(values["db_path"] or "").strip() or not db_path.is_absolute() or not db_path.is_file():
            raise ValueError("CMS 数据库必须是 MoviePilot 容器内已存在的绝对文件路径")
        directory_id = str(values["source_directory_id"] or "").strip()
        if not directory_id.isdigit():
            raise ValueError("请配置待整理目录的数字 ID")
        return cls(db_path=db_path.resolve(), source_directory_id=directory_id)


@dataclass(frozen=True)
class Candidate:
    """A direct child file or directory to pass to MoviePilot."""

    file_id: str
    parent_id: str


class CmsDatabase:
    """Keep a short-lived, read-only SQLite connection, including live WAL data."""

    def __init__(self, path: Path):
        self.path = path
        self.connection: Optional[sqlite3.Connection] = None

    def __enter__(self) -> "CmsDatabase":
        self.connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA query_only = ON")
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(lift_list)")}
            if not {"id", "behavior_type", "item_json", "create_time"}.issubset(columns):
                raise ValueError("CMS lift_list 缺少 id/behavior_type/item_json/create_time 字段")
        except Exception:
            self.connection.close()
            self.connection = None
            raise
        return self

    def __exit__(self, *_args) -> None:
        self.connection.close()
        self.connection = None

    def events_in_window(self, start: datetime, end: datetime) -> list[dict]:
        """Read [start, end), newest first, and close the cursor before transfer."""
        with closing(self.connection.execute(
            "SELECT id, behavior_type, item_json FROM lift_list "
            "WHERE create_time >= ? AND create_time < ? ORDER BY create_time DESC, id DESC",
            (start.isoformat(sep=" "), end.isoformat(sep=" ")),
        )) as rows:
            return [dict(row) for row in rows]


class Organizer:
    """Process one window; failures are logged and discarded, never persisted."""

    def __init__(self, database: CmsDatabase, options: Options,
                 transfer: Callable[[Candidate], tuple[bool, str]],
                 stopped: Callable[[], bool] = lambda: False):
        self.database, self.options = database, options
        self.transfer, self.stopped = transfer, stopped

    def _handle(self, event: dict, seen: set[str]) -> tuple[str, str, str]:
        item = json.loads(event["item_json"])
        if not isinstance(item, dict):
            raise ValueError("item_json 必须是单个文件或目录的 JSON 对象")
        if str(item.get("parent_id", "")) != self.options.source_directory_id:
            return "skipped", "父目录 ID 未匹配待整理目录（仅匹配直接子项）", ""
        key = str(item.get("file_id") or "")
        if key in seen:
            return "skipped", "本轮已处理该对象，不重复提交或重试", ""
        seen.add(key)
        file_id = str(item.get("file_id") or "")
        if not file_id.isdigit() or file_id == "0":
            raise ValueError("事件缺少有效的 file_id")
        candidate = Candidate(file_id, self.options.source_directory_id)
        success, message = self.transfer(candidate)
        return ("success", "整理完成", candidate.file_id) if success else (
            "failed", str(message or "MoviePilot 整理失败，本次丢弃"), candidate.file_id,
        )

    def poll(self, now: Optional[datetime] = None,
             on_record: Optional[Callable[[dict], None]] = None) -> dict:
        """Process all rows in the completed half-hour with only batch-local deduplication."""
        start, end = time_window(now)
        counts = {status: 0 for status in ("success", "failed", "skipped")}
        seen = set()
        for event in self.database.events_in_window(start, end):
            if self.stopped():
                break
            try:
                status, message, file_id = self._handle(event, seen)
            except Exception as error:
                status, message, file_id = "failed", f"本次丢弃：{error}", ""
            counts[status] += 1
            if on_record:
                on_record({"id": event["id"], "event": event["behavior_type"],
                           "status": status, "file_id": file_id, "message": message})
        return {"start": str(start), "end": str(end), "counts": counts}
