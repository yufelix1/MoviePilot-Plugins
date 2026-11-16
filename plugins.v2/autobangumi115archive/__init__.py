from __future__ import annotations

import re
import stat
from pathlib import Path, PurePosixPath
from threading import Event as StopEvent, Lock
from time import monotonic
from urllib.parse import urlsplit

import requests

from app.chain.storage import StorageChain
from app.core.event import Event, eventmanager
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


DEFAULTS = {
    "enabled": False,
    "url": "",
    "upload_path": "",
    "token": "",
    "path_mapping": "",
}


class ArchiveError(RuntimeError):
    pass


def absolute_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.startswith("/") or ".." in value.split("/"):
        raise ArchiveError("必须使用不含 .. 的绝对路径")
    return PurePosixPath(value)


def match_bangumi(items: list[dict], keyword: str) -> list[dict]:
    keyword = keyword.strip().casefold()
    active = [b for b in items if not b.get("archived") and not b.get("deleted")]
    def names(b):
        return [str(b.get(k) or "").strip().casefold() for k in ("official_title", "title_raw")]
    exact = [b for b in active if keyword in names(b)]
    return exact or [b for b in active if any(keyword in n for n in names(b))]


class AutoBangumi115Archive(_PluginBase):
    plugin_name = "AutoBangumi归档115"
    plugin_desc = "通过 /ab 上传番剧至115，归档、禁用RSS并删除种子及文件。"
    plugin_icon = "https://raw.githubusercontent.com/yufelix1/MoviePilot-Plugins/main/icons/AutoBangumi.png"
    plugin_version = "1.0.0"
    plugin_author = "yufelix"
    author_url = "https://github.com/yufelix1"
    plugin_config_prefix = "autobangumi115archive_"
    plugin_order = 2
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._lock = Lock()
        self._stopped = StopEvent()
        self._config = dict(DEFAULTS)
        self._pending = {}

    def init_plugin(self, config: dict = None):
        # 取消正在运行的旧任务，旧任务持有自己的退出事件和配置快照。
        self._stopped.set()
        self._config = {k: (config or {}).get(k, v) for k, v in DEFAULTS.items()}
        self._stopped = StopEvent()
        self._pending.clear()

    def get_state(self):
        return bool(self._config["enabled"])

    @staticmethod
    def get_command():
        return [{"cmd": "/ab", "event": EventType.PluginAction,
                 "desc": "番剧上传115并归档：/ab 番剧名", "category": "插件命令",
                 "data": {"action": "autobangumi115_archive"}}]

    def get_api(self):
        return []

    def get_page(self):
        return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                 "text": "Telegram 输入 /ab 番剧名。同名时按提示发送 /ab select 编号。"
                         "依次上传115、归档、禁用RSS、删除种子及文件；失败即停，插件不自动重试。"}}]

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
                        "model": "url",
                        "label": "AutoBangumi地址",
                    },
                ),
                column(
                    "VTextField",
                    {"model": "token", "label": "API令牌", "type": "password"},
                ),
            ],
            [
                column(
                    "VTextField",
                    {
                        "model": "upload_path",
                        "label": "115上传目录",
                    },
                ),
                column(
                    "VTextField",
                    {
                        "model": "path_mapping",
                        "label": "路径映射（AB路径:MoviePilot路径，每行一条）",
                        "placeholder": "/downloads/Bangumi:/media/Bangumi",
                    },
                ),
            ],
            [
                column(
                    "VAlert",
                    {
                        "type": "warning",
                        "variant": "tonal",
                        "text": "需先在 MoviePilot 配置115存储。/ab 成功后会删除本地种子和文件。"
                                "番剧目录不得与其他规则共用，聚合RSS不会自动禁用。",
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

    def stop_service(self):
        self._stopped.set()
        self._pending.clear()

    def _reply(self, data, text):
        self.post_message(channel=data.get("channel"), source=data.get("source"),
                          userid=data.get("user"), title=self.plugin_name, text=text)

    @staticmethod
    def _check(stopped):
        if stopped.is_set():
            raise ArchiveError("任务已取消")

    @staticmethod
    def _api(session, config, method, endpoint, **kwargs):
        # requests 默认适配器不重试；禁止重定向，避免令牌跟随跳转。
        url = config["url"].strip().rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
            raise ArchiveError("AutoBangumi地址无效")
        if parsed.path.rstrip("/") not in ("", "/api/v1"):
            raise ArchiveError("地址应为服务根地址或 /api/v1 地址")
        if not config["token"].strip():
            raise ArchiveError("请填写 API 令牌")
        if not url.endswith("/api/v1"):
            url += "/api/v1"
        try:
            with session.request(method, url + endpoint,
                                 headers={"Authorization": f"Bearer {config['token'].strip()}"},
                                 timeout=(10, 60), allow_redirects=False, **kwargs) as response:
                if not 200 <= response.status_code < 300:
                    raise ArchiveError(f"{method} {endpoint} 返回 HTTP {response.status_code}")
                result = response.json()
        except (requests.RequestException, ValueError) as error:
            # 不输出原始异常，防止令牌或请求信息泄漏。
            raise ArchiveError(f"{method} {endpoint} 请求失败（{type(error).__name__}）") from None
        if isinstance(result, dict):
            if result.get("status") is False or result.get("success") is False:
                raise ArchiveError(f"{method} {endpoint} 返回失败")
        elif not isinstance(result, list):
            raise ArchiveError(f"{method} {endpoint} 返回结构无效")
        if method != "GET" and (not isinstance(result, dict) or not (
                result.get("status") is True or result.get("success") is True
                or ("status" not in result and "success" not in result
                    and (result.get("msg_zh") or result.get("msg_en"))))):
            raise ArchiveError(f"{method} {endpoint} 未确认成功")
        return result

    @staticmethod
    def _list(value, name):
        if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
            raise ArchiveError(f"{name} 返回结构无效")
        return value

    @eventmanager.register(EventType.PluginAction)
    def command(self, event: Event):
        data = event.event_data or {}
        if data.get("action") != "autobangumi115_archive" or not self.get_state():
            return
        if not self._lock.acquire(blocking=False):
            self._reply(data, "已有归档任务执行中，请等待完成。")
            return
        stage = "查询番剧"
        stopped, config = self._stopped, dict(self._config)
        try:
            arg = str(data.get("arg_str") or "").strip()
            if not arg:
                self._reply(data, "用法：/ab FX战士久留美")
                return
            key = tuple(str(data.get(k) or "") for k in ("channel", "source", "user"))
            with requests.Session() as session:
                self._check(stopped)
                items = self._list(self._api(session, config, "GET", "/bangumi/get/all"), "番剧列表")
                selection = re.fullmatch(r"select\s+(\d+)", arg, re.IGNORECASE)
                if selection:
                    pending = self._pending.pop(key, None)
                    index = int(selection.group(1)) - 1
                    if not pending or monotonic() - pending[0] > 600 or not 0 <= index < len(pending[1]):
                        raise ArchiveError("选择已过期或编号无效，请重新发送 /ab 番剧名")
                    selected = pending[1][index]
                    candidates = [b for b in items if b.get("id") == selected and not b.get("archived") and not b.get("deleted")]
                else:
                    self._pending.pop(key, None)
                    candidates = match_bangumi(items, arg)
                if not candidates:
                    self._reply(data, "未找到未归档的匹配番剧。")
                    return
                if len(candidates) > 1:
                    self._pending = {k: v for k, v in self._pending.items() if monotonic() - v[0] <= 600}
                    self._pending[key] = (monotonic(), [b["id"] for b in candidates])
                    lines = [f"{i}. {b.get('official_title')} | {b.get('year') or '未知年份'} | "
                             f"第{b.get('season') or '?'}季 | {b.get('group_name') or '未知字幕组'}"
                             for i, b in enumerate(candidates, 1)]
                    self._reply(data, "匹配到多个番剧：\n" + "\n".join(lines) + "\n请在10分钟内发送 /ab select 编号")
                    return
                bangumi = candidates[0]
                stage = "核验目录、RSS与种子"
                files, root, torrents, rss_ids = self._prepare(session, config, bangumi, items)
                self._check(stopped)
                self._reply(data, f"开始处理 {bangumi['official_title']}：{len(files)} 个文件。")
                stage = "上传115"
                uploaded, skipped = self._upload(config, bangumi, files, root, stopped)
                self._check(stopped)
                # 删除前再次核验文件清单和大小/mtime，变化则停止。
                if self._scan(root) != files:
                    raise ArchiveError("上传期间本地文件发生变化")
                stage = "归档番剧"
                self._check(stopped)
                self._api(session, config, "PATCH", f"/bangumi/archive/{bangumi['id']}")
                stage = "禁用RSS"
                for rss_id in rss_ids:
                    self._check(stopped)
                    self._api(session, config, "PATCH", f"/rss/disable/{rss_id}")
                stage = "删除种子及文件"
                self._check(stopped)
                if self._scan(root) != files:
                    raise ArchiveError("清理前本地文件发生变化")
                for downloader_id, hashes in torrents.items():
                    self._check(stopped)
                    self._api(session, config, "POST", "/downloader/torrents/delete",
                              json={"hashes": hashes, "delete_files": True, "downloader_id": downloader_id})
                self._reply(data, f"{bangumi['official_title']} 处理成功：上传 {uploaded}，跳过 {skipped}；"
                                  "已归档、禁用RSS并删除种子及文件。")
        except Exception as error:
            # 只展示受控的业务错误，第三方异常可能携带凭证。
            reason = str(error) if isinstance(error, ArchiveError) else type(error).__name__
            logger.error(f"【{self.plugin_name}】{stage}失败：{reason}；停止后续步骤，不自动重试")
            self._reply(data, f"{stage}失败：{reason}。已停止，不自动重试；此前成功步骤已生效。")
        finally:
            self._lock.release()

    @staticmethod
    def _map_path(value, mapping):
        path = absolute_path(value)
        matches = []
        for line in mapping.splitlines():
            if not line.strip():
                continue
            source, sep, target = line.strip().partition(":")
            if not sep:
                raise ArchiveError("路径映射格式必须为 AB路径:MoviePilot路径")
            source, target = absolute_path(source), absolute_path(target)
            if path.is_relative_to(source):
                matches.append((len(source.parts), Path(target) / path.relative_to(source)))
        if not matches:
            raise ArchiveError(f"路径未命中映射：{path}")
        return max(matches, key=lambda x: x[0])[1]

    @staticmethod
    def _scan(root):
        files = {}
        pending = [root]
        while pending:
            path = pending.pop()
            try:
                # rglob 会忽略部分目录读取错误；显式遍历，确保扫描完整才允许清理。
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise ArchiveError(f"目录中包含符号链接：{path}")
                if stat.S_ISDIR(info.st_mode):
                    pending.extend(sorted(path.iterdir(), reverse=True))
                elif stat.S_ISREG(info.st_mode):
                    if info.st_size <= 0 or path.name.endswith((".!qB", ".part", ".tmp")):
                        raise ArchiveError(f"文件为空或尚未完成：{path}")
                    files[path] = (info.st_size, info.st_mtime_ns)
            except OSError as error:
                raise ArchiveError(
                    f"无法完整扫描番剧目录，读取失败：{path}（{type(error).__name__}）"
                ) from None
        if not files:
            raise ArchiveError("番剧目录为空")
        return files

    def _prepare(self, session, config, bangumi, items):
        source = absolute_path(bangumi.get("save_path"))
        root = self._map_path(str(source), config["path_mapping"])
        if root.is_symlink() or not root.is_dir() or root.resolve() != root:
            raise ArchiveError("映射后的番剧目录不存在或经过符号链接")
        # 不允许直接操作映射根目录。
        for line in config["path_mapping"].splitlines():
            if line.strip() and source == absolute_path(line.strip().partition(":")[0]):
                raise ArchiveError("番剧目录不能是路径映射根目录")
        for other in items:
            if other.get("id") == bangumi["id"] or not other.get("save_path"):
                continue
            other_path = absolute_path(other["save_path"])
            if source.is_relative_to(other_path) or other_path.is_relative_to(source):
                raise ArchiveError("番剧目录与其他规则重叠，无法独立归档")
        rss = self._list(self._api(session, config, "GET", "/rss"), "RSS列表")
        links = [r for r in rss if r.get("url") == bangumi.get("rss_link") and r.get("url")]
        if not links or any(r.get("aggregate") for r in links):
            raise ArchiveError("未找到独立RSS，或该RSS为聚合订阅")
        if any(b.get("id") != bangumi["id"] and not b.get("archived") and not b.get("deleted")
               and b.get("rss_link") == bangumi.get("rss_link") for b in items):
            raise ArchiveError("RSS仍被其他未归档番剧使用")
        all_torrents = self._list(self._api(session, config, "GET", "/downloader/torrents"), "种子列表")
        grouped = {}
        for torrent in all_torrents:
            tags = torrent.get("tags") or ""
            tags = tags if isinstance(tags, list) else re.split(r"[,\s]+", tags)
            ids = {tag[3:] for tag in tags if re.fullmatch(r"ab:\d+", tag)}
            save_path = absolute_path(torrent.get("save_path"))
            linked = str(bangumi["id"]) in ids
            in_dir = save_path.is_relative_to(source)
            content = absolute_path(torrent.get("content_path"))
            if not linked and not in_dir and not content.is_relative_to(source):
                continue
            if ids and ids != {str(bangumi["id"])}:
                raise ArchiveError("目录中有其他番剧的种子")
            if not content.is_relative_to(source):
                raise ArchiveError("种子内容路径不在番剧目录内，无法安全删除")
            if float(torrent.get("progress", 0)) != 1 or torrent.get("amount_left", 0) != 0:
                raise ArchiveError("存在未完成下载的种子")
            if not torrent.get("hash"):
                raise ArchiveError("种子缺少hash")
            grouped.setdefault(torrent.get("downloader_id"), []).append(torrent["hash"])
        if not grouped:
            raise ArchiveError("未找到番剧种子，无法完成清理")
        return self._scan(root), root, grouped, [int(r["id"]) for r in links]

    @staticmethod
    def _history_exists(records, upload_root, source):
        # 文件名取自当前番剧实际文件，转义 []、()、. 等正则特殊字符。
        prefix = re.escape(str(upload_root).rstrip("/"))
        pattern = re.compile(rf"{prefix}/(?:[^/]+/)*{re.escape(source.name)}")
        return any(
            record.status and record.src_storage == "u115"
            and isinstance(record.src, str) and pattern.fullmatch(record.src)
            for record in records
        )

    def _upload(self, config, bangumi, files, root, stopped):
        storage, history = StorageChain(), TransferHistoryOper()
        upload_root = absolute_path(config["upload_path"])
        destination = Path(upload_root) / root.name
        self._check(stopped)
        records = history.list_success_by_src(
            src=str(upload_root), storage="u115", recursive=True
        )
        uploaded = skipped = 0
        for path, fingerprint in files.items():
            self._check(stopped)
            target = destination / path.relative_to(root)
            if self._history_exists(records, upload_root, path):
                skipped += 1
                logger.info(f"【{self.plugin_name}】整理历史已存在，跳过：{path}")
                continue
            existing = storage.get_file_item(storage="u115", path=target)
            if existing:
                raise ArchiveError(f"115已有同名文件但无可信成功记录：{target}")
            folder = storage.get_folder(storage="u115", path=target.parent)
            if not folder:
                raise ArchiveError(f"创建115目录失败：{target.parent}")
            self._check(stopped)
            result = storage.upload_file(fileitem=folder, path=path)
            if not result or result.type != "file" or result.size != fingerprint[0]:
                raise ArchiveError(f"上传失败或返回大小不一致：{path}")
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != fingerprint:
                raise ArchiveError(f"上传期间文件发生变化：{path}")
            uploaded += 1
            logger.info(f"【{self.plugin_name}】上传成功：{path}")
        return uploaded, skipped
