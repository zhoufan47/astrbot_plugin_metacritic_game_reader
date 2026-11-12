# 自定义 HTML 模板

本目录下的每个 `.html` 文件都是一个可用于渲染游戏评分图片的模板。
把新模板（例如 `my_style.html`）放进本目录，重启/重载插件后即可在
AstrBot WebUI 的插件配置里选择，或用 `/评分模板 my_style` 切换。

## 内置模板

| 名称 | 说明 |
| --- | --- |
| `classic` | 浅色卡片式，蓝色主视觉 + 封面，信息分区清晰（默认） |
| `neon` | 深色赛博/霓虹风，青紫配色，等宽科技感标签 |
| `compact` | 报纸杂志风，双栏紧凑排版，竖排封面，信息密度最高 |
| `glass` | 通透玻璃风，半透明悬浮面板 + backdrop-filter 背景模糊 + 微亮描边与轻柔阴影 |

## 可用数据（Jinja2 变量）

模板使用 `HTML + Jinja2` 语法（`{% if %}` / `{% for %}` / `{{ }}`），渲染数据
为一个 `game` 对象，外加 `template_name`（当前模板名）和 `quote_chars`（评测节选字数）。

```
game.title              游戏英文标题
game.slug               Metacritic slug
game.query              用户输入的原始游戏名（识别前）
game.release_date       发行日期
game.premiere_year      发行年份
game.genres             类型列表 list[str]
game.developer          开发商
game.publisher          发行商
game.cover_url          封面图 URL（cardImage，竖版）
game.hero_url           主视觉图 URL（mainImage）
game.description        简介（已截断）
game.source_url         Metacritic 页面链接
game.platforms          平台列表（已按 PC / PS5 / NS2 / XSX 排序过滤）
  ├─ name                 平台名，如 "PC" / "PlayStation 5"
  ├─ slug                 平台 slug
  ├─ critic_score         媒体评分 int (0-100) 或 None
  ├─ critic_review_count  媒体评测数量 int 或 None
  ├─ critic_sentiment     媒体评价，如 "Universal acclaim"
  ├─ user_score           用户评分 float (0-10) 或 None
  ├─ user_review_count    用户评分数量 int 或 None
  ├─ user_sentiment       用户评价
  └─ reviews              前 N 篇媒体评测 list
       ├─ score            分数 int 或 None
       ├─ score_text       分数字符串（无分数时为 "–"）
       ├─ publication      媒体名
       ├─ quote            评测节选（前 N 字）
       └─ date             日期
```

## 约束

- 渲染由 AstrBot t2i（Playwright）完成，整页截图。请给 `body` 设定固定宽度，
  不要依赖外部字体、JS 或 CDN。
- 图片直接引用 Metacritic 公开 URL，渲染服务会联网加载。
- 分数配色可按 `>=75 / >=50 / else` 分档（用户评分建议按 `>=7.5 / >=5 / else`）。
- 文件名只能用字母、数字、下划线和连字符（不含 `.html` 后缀即为模板 ID）。
- 恶意/损坏的模板不会影响插件：加载失败会自动回退到 `classic` 并以文本兜底。

可以直接复制 `classic.html` 作为起点修改。
