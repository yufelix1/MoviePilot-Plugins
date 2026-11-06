"""Use MoviePilot services to organize one CMS window without persistent data."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from app.chain.transfer import TransferChain
from app.schemas import FileItem

from .core import Candidate, CmsDatabase, Options, Organizer
from .storage import source_items


class OrganizerService:
    """Keep storage lookups and transfer state within a single execution."""

    def __init__(self, options: Options, stopped: Callable[[], bool]):
        self.options = options
        self.stopped = stopped
        self._items: Optional[dict[str, FileItem]] = None
        self._lookup_error = ""

    def run(self, now: datetime, on_record: Callable[[dict], None]) -> dict:
        """Consume the supplied window using host directory and transfer settings."""
        self._items, self._lookup_error = None, ""
        with CmsDatabase(self.options.db_path) as database:
            return Organizer(database, self.options, self._transfer, self.stopped).poll(
                now=now, on_record=on_record,
            )

    def _transfer(self, candidate: Candidate) -> tuple[bool, str]:
        """Pass the matching FileItem to MoviePilot; directories use host recursion."""
        if self.stopped():
            return False, "插件已停止，本次丢弃"
        if self._items is None and not self._lookup_error:
            try:
                self._items = source_items(self.options)
            except Exception as error:
                self._lookup_error = str(error) or "查询待整理目录失败，本轮丢弃"
        if self._lookup_error:
            return False, self._lookup_error
        if self.stopped():
            return False, "插件已停止，本次丢弃"
        fileitem = self._items.get(candidate.file_id)
        if not fileitem:
            return False, "待整理目录内未找到事件对象，本次丢弃"
        success, message = TransferChain().do_transfer(
            fileitem=fileitem,
            background=False,
            force=False,
            continue_callback=lambda: not self.stopped(),
        )
        if self.stopped():
            return False, "插件停止时整理可能尚未完成，不保存或重试"
        return bool(success), str(message or "")
