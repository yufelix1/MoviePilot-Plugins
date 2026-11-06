"""Resolve live 115 objects through MoviePilot's configured resource directories."""

from pathlib import Path, PurePosixPath

from app.chain.storage import StorageChain
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.schemas import FileItem

from .core import Options


def _cloud_path(value: str) -> str:
    """Validate absolute paths returned by the host storage adapter."""
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("宿主网盘路径必须以 / 开头")
    if "\x00" in value or "\\" in value or ".." in value.split("/"):
        raise ValueError("宿主网盘路径包含无效字符或 ..")
    return str(PurePosixPath(value))


def source_items(options: Options) -> dict[str, FileItem]:
    """Match the configured source ID, then list its direct children once per run."""
    chain = StorageChain()
    checked, matches, errors = set(), [], []
    for config in DirectoryHelper().get_download_dirs():
        if config.storage not in {"u115", "115网盘Plus"} or not config.monitor_type:
            continue
        try:
            path = _cloud_path(config.download_path)
            key = (config.storage, path)
            # Media-specific routes can share the same resource directory.
            if key in checked:
                continue
            checked.add(key)
            directory = chain.get_file_item(storage=config.storage, path=Path(path))
            if not directory or str(directory.fileid) != options.source_directory_id:
                continue
            if directory.type != "dir" or _cloud_path(directory.path) != path:
                raise ValueError("宿主资源目录类型或路径不一致")
            matches.append((path, directory))
        except Exception as error:
            errors.append(str(error))
    if not matches:
        detail = f"：{'；'.join(errors)}" if errors else ""
        raise ValueError("未找到待整理目录 ID 对应的宿主 115 资源目录，请在 MoviePilot 目录设置中配置并启用整理" + detail)
    if len(matches) != 1:
        raise ValueError("多个宿主存储或资源路径匹配待整理目录 ID，无法唯一确定来源，本轮丢弃")
    path, directory = matches[0]
    items = chain.list_files(directory, recursion=False)
    if items is None:
        raise ValueError("网盘待整理目录查询失败，本轮丢弃")
    result = {}
    for item in items:
        try:
            if str(item.parent_fileid) != options.source_directory_id:
                continue
            if str(PurePosixPath(_cloud_path(item.path)).parent) != path:
                continue
            result[str(item.fileid)] = item
        except (AttributeError, TypeError, ValueError) as error:
            name = getattr(item, "name", None) or getattr(item, "path", None) or "未提供名称"
            logger.warning(f"跳过异常网盘子项 {name!r}（ID={getattr(item, 'fileid', '')}）：{error}")
    return result
