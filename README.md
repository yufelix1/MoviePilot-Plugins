# MoviePilot-Plugins

MoviePilot 第三方插件仓库，适用于 MoviePilot V2。

```text
https://github.com/yufelix1/MoviePilot-Plugins
```

## 插件合集

| 插件 | 功能 | MoviePilot 版本 |
| --- | --- | --- |
| [CMS115 自动整理](#cms115-自动整理) | 基于 CMS 同步事件，自动识别并整理 115 网盘。 | `>=2.12.0,<3` |
| [AutoBangumi归档115](#AutoBangumi归档115) | Telegram `/ab` 上传番剧到115，归档、禁用RSS并删除种子及文件。 | `>=2.12.0,<3` |

## CMS115 自动整理

插件 ID：`CMS115Organizer`，源码位于 [`plugins.v2/cms115organizer`](./plugins.v2/cms115organizer)。

每小时 25、55 分读取 CMS 数据库中的上一段半小时事件，将待整理目录中的文件或目录交给 MoviePilot 整理。媒体识别、目标目录、整理方式、重命名及元数据刮削均复用 MoviePilot 的配置。

## AutoBangumi归档115

插件 ID：`AutoBangumi115Archive`，源码位于 [`plugins.v2/autobangumi115archive`](./plugins.v2/autobangumi115archive)。

在 Telegram 发送 `/ab 番剧名`，将番剧文件上传至 115，随后归档 AutoBangumi 规则、禁用 RSS，并删除下载器中的种子及本地文件。
