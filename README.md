# astrbot_plugin_iwara

Iwara (`www.iwara.tv`) AstrBot 插件，支持：

- 视频/图片搜索
- 视频详情、图片详情查询
- 视频直链解析（按报告中的 `X-Version` 算法）
- 相关视频、评论列表、点赞用户查询
- 热门内容（视频/图片）
- 用户资料查询
- 用户视频/图片列表浏览
- 用户粉丝/关注列表查看
- 图文消息链返回（搜索、详情、直链尽量附封面）
- 可选代理与图片打码等级配置
- **订阅博主更新**（定时轮询，有新视频/图片自动通知）

## 命令

- `/iwara_search [video|image|all] <关键词>`
- `/iwara_video <视频ID或链接>`
- `/iwara_image <图片ID或链接>`
- `/iwara_direct <视频ID或链接>`
- `/iwara_related <视频ID或链接>`
- `/iwara_comments <视频ID或链接>`
- `/iwara_likes <视频ID或链接>`
- `/iwara_trending [video|image|all]`
- `/iwara_user <用户名>`
- `/iwara_uservideos <用户名>` — 查看用户视频列表
- `/iwara_userimages <用户名>` — 查看用户图片列表
- `/iwara_followers <用户名>` — 查看用户粉丝列表
- `/iwara_following <用户名>` — 查看用户关注列表
- `/iwara_probe`
- `/iwara_diag`
- **订阅相关：**
  - `/iwara_sub <用户名>` — 订阅博主更新
  - `/iwara_unsub <用户名>` — 取消订阅
  - `/iwara_sublist` — 查看当前订阅列表

示例：

- `/iwara_search all miku`
- `/iwara_video vsGv0RRqM4mVhE`
- `/iwara_direct https://www.iwara.tv/video/vsGv0RRqM4mVhE`
- `/iwara_related vsGv0RRqM4mVhE`
- `/iwara_comments vsGv0RRqM4mVhE`
- `/iwara_likes vsGv0RRqM4mVhE`
- `/iwara_trending video`
- `/iwara_user nightmate71`
- `/iwara_uservideos nightmate71` — 查看用户的所有视频
- `/iwara_userimages nightmate71` — 查看用户的所有图片
- `/iwara_followers nightmate71` — 查看用户的粉丝
- `/iwara_following nightmate71` — 查看用户的关注
- `/iwara_sub nightmate71` — 订阅博主
- `/iwara_sublist` — 查看订阅列表
- `/iwara_probe`

## 配置

通过 `_conf_schema.json` 暴露配置项：

- `proxy_url`: 可选 HTTP/HTTPS 代理地址，留空为直连。
- `request_engine`: 请求引擎，支持 `auto / aiohttp / cloudscraper`（默认 `auto`，遇 Cloudflare 403 会尝试 cloudscraper 回退）。
- `image_transport`: 图片发送方式，支持 `bytes / url`（默认 `bytes`，更稳定，避免 OneBot 远程拉图超时）。
- `image_fetch_timeout_sec`: 图片下载超时秒数（3-30）。
- `image_max_kb`: 单图最大体积（KB）。
- `image_censor_level`: 返回图片打码程度，支持 `off / low / medium / high`（`bytes` 模式下本地打码，不依赖第三方图床）。
- `search_limit`: 搜索最多返回条数（1-10）。
- `subscribe_poll_interval_min`: 订阅轮询间隔，分钟（5-1440，默认30分钟）。
- `request_timeout_sec`: 请求超时秒数（5-60）。
- `request_user_agent`: 请求 UA（默认已内置浏览器 UA）。
- `request_referer`: 请求 Referer（默认 `https://www.iwara.tv/`）。
- `request_origin`: 请求 Origin（默认 `https://www.iwara.tv`）。
- `request_cookie`: 可选 Cookie（如 `cf_clearance=...`）。
- `request_bearer_token`: 可选 Bearer Token（浏览器 localStorage 中的访问令牌）。
- `warmup_homepage`: 发送 API 前先访问首页预热会话（建议开启）。
- `api_base_url` / `file_api_base_url` / `site_host`: API 与站点域名配置。

## 说明

- 直链解析基于 `iwara.md` 报告中的流程：从 `fileUrl` 提取 `fileId/expires/hash`，计算 `X-Version(SHA-1)` 后请求 `files.iwara.tv/file/{fileId}`。
- `image_transport=bytes` 时会在插件内对图片字节做本地模糊（low/medium/high）；`url` 模式下仅发送原图 URL（不做打码）。
- **订阅机制**：插件启动后注册定时任务，按 `subscribe_poll_interval_min` 间隔轮询已订阅用户的视频和图片列表。首次轮询记录所有已知ID，后续仅推送新发现的视频/图片。无需认证即可检测更新。

## 403 排障（Cloudflare）

若日志出现 `Just a moment...` 或 `HTTP 403 (Cloudflare challenge)`：

1. 优先配置 `proxy_url`（可稳定访问 Iwara 的代理）。
2. 可补充 `request_cookie`（浏览器登录态 + `cf_clearance`）。
3. 保持 `request_user_agent` 与 `request_referer` 为浏览器值（默认已提供）。
4. 可用 `/iwara_diag` 检查插件是否读取到 Cookie 与当前代理状态。

## 一键导出本机配置（推荐）

已提供脚本：[scripts/export_iwara_runtime_config.py](scripts/export_iwara_runtime_config.py)

用途：在本机打开可见浏览器，手动登录 Iwara 后一键导出可粘贴到 VPS 的插件配置（Cookie/UA/Token）。

运行：

```bash
python -m pip install playwright
python -m playwright install chromium
python scripts/export_iwara_runtime_config.py
```

如需启用 `cloudscraper` 引擎：

```bash
python -m pip install cloudscraper
```

如需启用 `image_censor_level=low|medium|high` 的本地打码能力：

```bash
python -m pip install pillow
```

可选参数：

```bash
python scripts/export_iwara_runtime_config.py --proxy http://127.0.0.1:7890 --browser chrome
```

脚本只导出：

- `iwara_runtime_config.txt`（一行一个字段，格式如 `Referer: ...`，便于手工复制）

## 相关链接

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档（中文）](https://docs.astrbot.app/dev/star/plugin-new.html)
