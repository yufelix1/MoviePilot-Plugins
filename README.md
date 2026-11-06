# MoviePilot-Plugins

MoviePilot 第三方插件仓库，适用于 MoviePilot V2。

```text
https://github.com/yufelix1/MoviePilot-Plugins
```

## 插件合集

| 插件 | 功能 | MoviePilot 版本 |
| --- | --- | --- |
| [CMS115 自动整理](#cms115-自动整理) | 基于 CMS 同步事件，自动识别并整理 115 网盘。 | `>=2.12.0,<3` |

## CMS115 自动整理

插件 ID：`CMS115Organizer`，源码位于 [`plugins.v2/cms115organizer`](./plugins.v2/cms115organizer)。

每小时 25、55 分读取 CMS 数据库中的上一段半小时事件，将待整理目录中的文件或目录交给 MoviePilot 整理。媒体识别、目标目录、整理方式、重命名及元数据刮削均复用 MoviePilot 的配置。
