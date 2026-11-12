# Metacritic 游戏评分查询（AstrBot 插件）

输入 `/评分 XX` 或 `/mc XX`，插件会**智能识别 XX 可能的游戏名称**，然后去
[Metacritic](https://www.metacritic.com/) 抓取该游戏在**多个平台**上的评分数据，
把封面图、主视觉图、媒体评分、媒体评测数量、用户评分、前 5 篇媒体评测的分数和前 100 字
组装进 HTML 模板，通过 AstrBot 的 `t2i` 文转图接口渲染成图片发送给用户。

## 功能

- 🧠 **智能识别游戏名**：优先调用 AstrBot 会话所用的大模型，把中文俗称、别名、
  缩写、错拼规范化成 Metacritic 的英文官方名（如「老头环」→ `Elden Ring`、
  「黑神话悟空」→ `Black Myth: Wukong`）；模型不可用时自动降级到内置中文别名表
  和英文原名搜索。
- 🎮 **多平台评分**：只抓取并展示 `PC`、`PlayStation 5`、`Nintendo Switch 2`、
  `Xbox Series X` 四个平台（按此优先级排序），其余平台（PS4 / Xbox One / Switch 等）
  一律忽略。每个平台各自包含：
  - 媒体评分（Metascore）与媒体评测数量
  - 用户评分（User Score）与用户评分数量
  - 前 5 篇媒体评测的分数、媒体名、日期与前 100 字节选
- 🖼️ **多套 HTML 模板**：模板是 `templates/` 目录下的独立 `.html` 文件，内置
  3 套风格（经典卡片 / 霓虹赛博 / 报纸杂志），可自由切换，也可放入自定义模板。
  封面图（cardImage）+ 主视觉图（mainImage）+ 全部评分信息组装成卡片，通过
  AstrBot `html_render`（t2i）渲染为图片发送。渲染服务不可用时自动降级为纯文本。
- 🎨 **模板切换**：WebUI 配置项或 `/评分模板` 命令，支持仅当前会话生效、
  全局持久化生效、以及重置回 WebUI 配置。
- ⚙️ **可配置**：模板、是否启用 LLM 识别、评测条数、节选字数、请求超时，见
  `_conf_schema.json`，可在 AstrBot WebUI 插件配置页直接修改。

## 使用

```
/评分 艾尔登法环
/mc Elden Ring
/评分 黑神话悟空
/mc 丝之歌
/评分 Hollow Knight Silksong
```

`/评分` 与 `/mc` 是同一条指令的两个名字。游戏名支持带空格（如 `Elden Ring`）。

### 切换渲染模板

```
/评分模板              查看全部模板与当前生效模板
/评分模板 neon         仅当前会话切换到 neon 模板
/评分模板 compact 全局  全局持久化切换（写入插件 KV，所有会话生效）
/评分模板 重置          清除会话/全局覆盖，回到 WebUI 配置
/mc模板 列表           （别名）列出全部模板
```

也可以在 AstrBot WebUI → 插件 → 本插件的配置里直接选 `template` 下拉框。

内置模板：

| ID | 风格 |
| --- | --- |
| `classic` | 浅色卡片式，蓝色主视觉 + 封面，分区清晰（默认） |
| `neon` | 深色赛博/霓虹风，青紫配色，科技感标签 |
| `compact` | 报纸杂志风，双栏紧凑排版，信息密度最高 |
| `glass` | 通透玻璃风，半透明悬浮面板，背景模糊、微亮边缘与轻柔阴影 |

**自定义模板**：把一个 `xxx.html` 放进插件的 `templates/` 目录，重载插件后即可
用 `/评分模板 xxx` 或（把它加进配置下拉框后）在 WebUI 里选择。模板使用
`HTML + Jinja2`，可用变量详见 [templates/README.md](templates/README.md)。
模板名只允许字母、数字、下划线、连字符；损坏的模板会自动回退到 `classic`。

## 安装

1. 把本目录放到 AstrBot 的 `data/plugins/` 下（目录名建议保持
   `astrbot_plugin_metacritic_game_reader`）。
2. 在 AstrBot WebUI → 插件页点击「重载插件」；依赖 `aiohttp` 会按
   `requirements.txt` 自动安装（AstrBot 通常已自带）。
3. 确保已配置 t2i 文转图服务（AstrBot 默认使用官方公共端点，也可自部署）；
   若想用大模型识别中文游戏名，请确保已配置至少一个聊天模型提供商。

## 配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `template` | `classic` | 渲染模板 ID（可选 `classic` / `neon` / `compact` 或自定义模板） |
| `use_llm` | `true` | 用会话大模型智能识别游戏名；关闭则只用内置别名表/英文原名 |
| `max_reviews` | `5` | 每个平台展示的媒体评测篇数（1–10） |
| `review_quote_chars` | `100` | 每篇评测节选字数（「前 100 字」） |
| `request_timeout` | `20` | 单次 HTTP 请求超时（秒） |

## 数据来源与实现说明

Metacritic 官网是 Nuxt 应用，页面数据来自其公开 JSON 后端
`https://backend.metacritic.com`。本插件静态读取以下已验证端点，不执行目标页面脚本：

| 用途 | 端点 |
| --- | --- |
| 搜索游戏 | `/finder/metacritic/search/{关键词}/web?...&mcoTypeId=13` |
| 游戏详情（标题/封面/主视觉/平台列表） | `/games/metacritic/{slug}/web?componentName=product` |
| 平台媒体评分 | `/reviews/metacritic/critic/games/{slug}/platform/{平台}/stats/web` |
| 平台用户评分 | `/reviews/metacritic/user/games/{slug}/platform/{平台}/stats/web` |
| 平台媒体评测列表 | `/reviews/metacritic/critic/games/{slug}/platform/{平台}/web?...&limit=10` |

要点：

- 搜索索引只覆盖英文/罗马字，中文关键词必须先转成英文官方名，否则返回无关结果。
- `?platform=pc` 查询参数会被后端**忽略**；平台必须通过路径段
  `/platform/{slug}/` 指定，否则拿到的是「主平台」的数据。这是多平台数据正确性的关键。
- 媒体评测数量以 `product.platforms[].criticScoreSummary.reviewCount` 为准
  （即站点平台卡片上 "Based on N Critic Reviews" 的数值）。
- 图片 URL 由 `images[].bucketPath` 拼成
  `https://www.metacritic.com/a/img/catalog` + `bucketPath`；
  `mainImage` 为主视觉，`cardImage` 为封面。

参考/灵感来源（均为只读调研，未复用其代码）：

- Metacritic 官网公开后端接口
- [omkarcloud/metacritic-scraper](https://github.com/omkarcloud/metacritic-scraper)
- [electrocow0-tech/metacritic-games](https://github.com/electrocow0-tech/metacritic-games)

## 文件结构

```
main.py                插件入口：指令、智能识别、模板选择、t2i 渲染与消息发送
metacritic_client.py   Metacritic 异步数据抓取客户端
template.py            HTML 模板加载器（发现/读取/校验）与模板数据组装
templates/             独立的 .html 模板文件
  ├─ classic.html      经典卡片（默认）
  ├─ neon.html         霓虹赛博
  ├─ compact.html      报纸杂志
  ├─ glass.html        通透玻璃
  └─ README.md         自定义模板与可用变量说明
_conf_schema.json      AstrBot 插件配置 Schema
metadata.yaml          插件元数据
requirements.txt       依赖声明
```

## 许可

MIT（沿用模板仓库许可）。插件仅做数据聚合展示，评分与评测版权归 Metacritic 及
各媒体所有。
