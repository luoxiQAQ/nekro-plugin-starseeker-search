# 星巡搜索

星巡搜索是一个面向 Nekro Agent 的联网搜索插件。它把外部搜索结果整理成 Agent 可引用的标题、摘要和 URL，用于回答新闻、版本、活动、价格、汇率、资料查证等需要外部信息的问题；同时提供以图搜图能力，用于查找图片的来源、原作者和出处链接。

插件只使用 Python 标准库实现文字搜索，不依赖 `requests`、`httpx`、`openai` 或 `bs4`，适合最小化运行容器和无法动态安装依赖的环境。以图搜图部分依赖 `playwright` 与 Chromium（见下文依赖说明）。

> 本项目是基于 [Akiyo-dayo/nekro-plugin-starseeker-search](https://github.com/Akiyo-dayo/nekro-plugin-starseeker-search) 的改良版，感谢原作者的工作。主要改进集中在以图搜图：百度识图跟进详情页提取真实来源、trace.moe 噪音过滤、Chromium 自动定位与容器重建自动恢复。

## 功能

- 为 Agent 提供 `web_search` 沙箱方法。
- 为 Agent 提供 `image_search` 沙箱方法（以图搜图）。
- 文字搜索支持 Tavily、Brave Search、自建 SearXNG。
- 在未配置正式搜索 API 时，提供有限的无 key fallback。
- 对搜索结果进行去重、关键词相关性评分和低价值域名降权。
- 当结果可信度不足时返回明确失败原因，避免 Agent 用弱相关链接编造答案。

## 工具接口

### web_search

```python
web_search(query: str, max_results: int = 5) -> str
```

参数：

- `query`: 要搜索的关键词、问题、新闻主题或网页主题。
- `max_results`: 返回结果数量，范围 `1-10`。不传时使用插件配置里的 `MAX_RESULTS`。

返回内容包含：

- 搜索主题
- 使用的结果来源
- 多条搜索结果的标题、摘要和 URL
- 搜索失败或低可信时的原因说明

### image_search

```python
image_search(image_path: str, max_results: int = 3) -> str
```

参数：

- `image_path`: 图片的沙箱路径，如 `/app/shared/xxx.jpg`。
- `max_results`: 返回结果数量，范围 `1-10`。不传时使用插件配置里的 `IMAGE_SEARCH_MAX_RESULTS`。

返回内容包含：

- 来源站点、相似度（引擎提供时）
- 原作者（引擎提供时）
- 出处页面链接与补充信息

## 文字搜索

### 推荐配置

生产环境建议配置正式搜索 API。推荐顺序：

1. `Tavily`: 适合 Agent 工作流，结果摘要对问答场景友好。
2. `Brave`: 通用网页搜索 API，适合需要传统搜索结果列表的场景。
3. `SearXNG`: 适合自建搜索网关；不建议依赖公共实例。
4. `fallback`: 无 key 临时兜底，不应作为稳定搜索能力。

### 配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `PROVIDER` | `auto` | 搜索提供方。支持 `auto`、`tavily`、`brave`、`searxng`、`duckduckgo`、`bing`、`fallback`。 |
| `TAVILY_API_KEY` | 空 | Tavily API key。 |
| `BRAVE_API_KEY` | 空 | Brave Search API key。 |
| `SEARXNG_BASE_URL` | 空 | 自建 SearXNG 地址，例如 `https://search.example.com`。实例需要开放 JSON 搜索。 |
| `MAX_RESULTS` | `5` | 默认返回结果数量，范围 `1-10`。 |
| `TIMEOUT_SECONDS` | `12` | 单次请求超时时间，范围 `3-60` 秒。 |
| `ALLOW_BING_FALLBACK` | `true` | 允许无 key fallback。名称沿用旧配置，实际作为无 key 搜索源的总开关。 |

`PROVIDER=auto` 的行为：

- 有 `TAVILY_API_KEY` 时尝试 Tavily。
- 有 `BRAVE_API_KEY` 时尝试 Brave。
- 有 `SEARXNG_BASE_URL` 时尝试 SearXNG。
- 如果允许 fallback，再尝试无 key fallback。

正式 API 返回的结果会保留服务端排序，仅做 URL 去重和基础可用性检查。无 key fallback 返回的网页抓取结果会额外经过相关性过滤。

当正式 API 返回认证或权限错误，例如无效 key、过期 key、订阅权限不足或 `401/403`，插件会继续尝试后续已配置的正式搜索源，但不会静默降级到无 key fallback。普通网络故障或临时服务错误在 `auto` 模式下可以继续尝试后续搜索源；如果最终由 fallback 返回结果，输出中会包含降级提示。

## 以图搜图

以图搜图按引擎顺序执行，`IMAGE_SEARCH_PROVIDER=auto` 时依次尝试：

1. **SauceNAO** — 动漫图站聚合（Pixiv / Danbooru / Twitter 等），对 P 站等海外图站命中率高；最高相似度 ≥ 85% 时直接采用，不再继续其他引擎。
2. **IQDB** — 多 booru 站聚合，无 key 抓取。
3. **trace.moe** — 动画截图识别（番剧 + 集数 + 时间点）。对非动画截图会返回大量 70-80% 的近似噪音，因此相似度门槛最低取 85%。
4. **百度识图** — 通过浏览器上传 `graph.baidu.com`，擅长识别国内画师作品和角色。插件会跟进"相似图片"详情页，提取精确的识别结论（"图中可能是XXX"）和微博、抖音、百家号等真实来源页链接。

各引擎结果合并时，百度识图的识别结论与来源页保底收录并排在最前，避免被其他引擎的低置信度结果挤出返回列表；剩余名额按相似度降序补足。这个策略让国内图片优先呈现百度的识别与出处，海外图站图片由 SauceNAO 高置信命中直接收口。

### 依赖

以图搜图需要容器内具备：

- Python 包 `playwright`（在 Nekro Agent 的运行环境中）。
- Chromium 浏览器二进制。Debian 系容器可用 `apt-get install chromium`；或使用 `playwright install chromium` 安装 Playwright 自带浏览器。

插件会自动按以下顺序定位浏览器：Playwright 自带浏览器路径（受 `PLAYWRIGHT_BROWSERS_PATH` 影响）→ `/usr/bin/chromium` → `/usr/bin/chromium-browser` → `/usr/bin/google-chrome` → `/opt/google/chrome/chrome`。

容器重建后浏览器容易丢失，建议在容器 entrypoint 中加入自动安装，例如：

```bash
/app/.venv/bin/python -c "import playwright" >/dev/null 2>&1 || \
  uv pip install --python /app/.venv/bin/python -q playwright
if ! [ -x /usr/bin/chromium ]; then
  dpkg --configure -a >/dev/null 2>&1 || true
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends --fix-missing chromium || true
fi
```

### 配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `IMAGE_SEARCH_PROVIDER` | `auto` | 搜图引擎。支持 `auto`、`saucenao`、`iqdb`、`tracemoe`、`baidu`。 |
| `SAUCENAO_API_KEY` | 空 | SauceNAO API key，https://saucenao.com/user.php 注册获取（免费 200 次/天）。未配置时插件仍会通过浏览器抓取 SauceNAO 页面。 |
| `IMAGE_SEARCH_MIN_SIMILARITY` | `55.0` | 相似度过滤门槛（%），低于该值的结果被丢弃。trace.moe 实际门槛不低于 85%。 |
| `IMAGE_SEARCH_MAX_RESULTS` | `3` | 默认返回结果数量，范围 `1-10`。 |

## 无 Key Fallback

无 key fallback 是临时兜底，不是稳定搜索服务。它会优先使用相对可靠的通用搜索结果，再用低置信度来源补充：

- 中文查询：Bing HTML、Bing RSS、DuckDuckGo Lite、360 Search。
- 非中文查询：DuckDuckGo Lite、Bing HTML、Bing RSS。

注意事项：

- Bing 和 DuckDuckGo 的公开页面结构可能变化，解析结果不能等同正式 API。
- 360 Search 结果置信度较低，仅作为中文查询的补充来源。
- 公共 SearXNG 实例常见 `403`、`429` 或反机器人页，不适合作为默认后端。
- 对非中文查询，如果 fallback 只得到 Bing 的低可信结果，插件会返回失败提示，避免 Agent 据此编造答案。

## 使用建议

Agent 调用示例：

```python
web_search(query="OpenAI latest model news", max_results=5)
image_search(image_path="/app/shared/received_image.jpg")
```

建议：

- 当用户问"最新""今天""是否发布""有没有联动""价格""版本发布时间"等问题时，先调用 `web_search`。
- 当用户发送图片并询问出处、原作者、原图时，调用 `image_search`。
- 回答时引用结果中的 URL，不要只凭摘要下结论。
- 如果返回"未找到可靠结果"或"低可信"，应直接告诉用户搜索源不足。
- 对金融、医疗、法律、重大新闻等高风险问题，即使搜索成功也应提示来源和时效限制。
- 以图搜图结果中，带相似度百分比的是搜索引擎的匹配打分，`百度识别` 条目是图像内容识别结论，两者性质不同，回答时应区分引用。

## 故障排查

### 插件没有加载

检查插件详情里是否满足：

- `name`: `星巡搜索`
- `moduleName`: `nekro_plugin_starseeker_search`
- `enabled`: `true`
- `loadFailed`: `false`
- `methods` 中存在 `web_search` 与 `image_search`

### 返回结果答非所问

优先检查：

1. 是否配置了 `TAVILY_API_KEY`、`BRAVE_API_KEY` 或 `SEARXNG_BASE_URL`。
2. 当前 `PROVIDER` 是否被固定为 `bing` 或 `fallback`。
3. 查询词是否太宽泛。建议包含关键实体和限定词。
4. 是否依赖无 key fallback。无 key fallback 不保证稳定结果质量。

如果已配置正式 API 但返回中出现无 key 来源，先检查输出中的降级提示。认证或权限错误会阻止静默降级；普通网络错误、超时或服务端临时错误可能触发后续 provider。

### 正式 API 返回认证或权限错误

检查对应服务的 key 是否有效、是否已启用搜索权限、是否超出套餐限制，以及容器配置是否已保存并在重启后生效。插件不会输出 key 内容；排查时只需要确认 key 是否存在、长度是否符合预期、服务端是否接受该 key。

### DuckDuckGo 报 TLS 错误

这是容器网络到 DuckDuckGo Lite 的握手问题。插件会继续尝试其他 fallback 源，但结果质量可能下降。生产环境应配置 Tavily、Brave 或自建 SearXNG。

### 以图搜图报"未找到 Chromium 浏览器"或"需要 playwright"

说明容器内缺少以图搜图依赖。检查：

1. Nekro Agent 运行环境能否 `import playwright`，不能则 `uv pip install playwright`。
2. 容器内是否有 Chromium：`ls /usr/bin/chromium`，没有则 `apt-get install chromium` 或 `playwright install chromium`。
3. 容器重建后依赖会丢失，建议把自动安装写入 entrypoint（见上文依赖一节）。

### 国内图片搜不到或结果不准

国内画师作品主要依赖百度识图引擎。确认 `IMAGE_SEARCH_PROVIDER` 为 `auto` 或 `baidu`，并确认容器内 Chromium 可用。百度识图结果不提供相似度百分比，但会给出识别结论和微博、抖音等真实来源页；如果百度页面出现安全验证或页面结构变更，结果质量可能下降。

## 维护说明

- 仓库地址：https://github.com/luoxiQAQ/nekro-plugin-starseeker-search
- 插件目录：`plugins/packages/nekro_plugin_starseeker_search`
- 配置目录：`plugin_data/Akiyo_Codex.nekro_plugin_starseeker_search`
- 插件作者字段必须只包含字母、数字和下划线，所以使用 `Akiyo_Codex`。
- 修改代码后需要重启 Nekro Agent 才能让聊天侧加载新版本。
