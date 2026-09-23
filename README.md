# 聚合搜索中心 (astrbot_plugin_websearch_hub)

把搜索做成插件侧聚合层：LLM 只看到一个 `web_search` 函数工具，插件内部按优先级轮询多个搜索提供商，失败自动回退。**与当前对话模型无关**——主模型回退到任意国模后搜索依然可用。

## 预设搜索源

| 模板 | 说明 |
|---|---|
| Gemini 谷歌原生搜索 | 调 Gemini API 的 `google_search` grounding |
| Grok 原生搜索 | 调 xAI chat/completions 的 `search_parameters` |
| GPT 原生搜索 | 调 OpenAI Responses API 的 `web_search` 内置工具 |
| Tavily 搜索 | 调 Tavily Search API |

每个提供商支持：自定义名称、**请求协议可选**（`auto` / `gemini_native` / `openai_responses` / `openai_chat_search` / `tavily`）、**可直接选择 AstrBot 已配置的模型提供商**（自动继承其 API 地址、Key 列表和模型名，protocol=auto 时按适配器类型自动推断协议）、可改 base_url（中转友好）、**多个 API Key 轮询**、独立超时、独立最大结果数、单独启用/禁用。

> 例：某中转站把 Gemini 的谷歌搜索包装成了 OpenAI Responses 格式，那就选 `openai_responses` 协议 + 中转 base_url + gemini 模型名即可；如果该中转已在 AstrBot 里配置为提供商，直接在下拉框选它更省事。

## 行为规则

- 列表顺序即优先级：第一个源失败（网络错误/鉴权失败/限流）→ 换下一个源。
- 同一源内多个 Key 轮询；失败的 Key 进入冷却期（默认 300 秒），冷却结束自动恢复。
- **搜索成功但无结果时不会换源**（避免浪费配额），空结果原样返回给模型。
- 所有源均失败时返回明确的错误汇总文本。

## 工具

- `web_search(query)` — 聚合搜索，返回摘要 + 来源列表。
- `fetch_web_page(url)` — 抓取网页正文（自动去标签；默认不截断，可在配置里设最大字符数）。

## 管理面板与配置推荐

> **💡 推荐进入 Dashboard 管理面板进行配置与管理**
> AstrBot 原生配置面板对嵌套/复杂列表（尤其是提供商拖拽排序、测试源连通性等）的操作体验有限。
> **强烈推荐**直接点击 AstrBot 面板的 **插件管理 → 本插件 → 页面** 进入专属 Dashboard 页面：
> - 轻松添加 / 删除 / **一键上下箭头调整优先级顺序**（列表顺序即轮询回退优先级）
> - 服务卡片折叠设计，协议、Base URL、模型、API Key 轮询列表清晰易配
> - 一键下拉选择 AstrBot 已配置的模型提供商（自动继承地址/Key/模型与推断协议）
> - 内置 **「🧪 测试此搜索源」** 按钮，免去盲测，现场即可验证连通性与检索效果

## 使用建议

在 AstrBot 面板关闭内置网页搜索（`provider_settings.websearch_provider`），避免与本插件工具重复。
