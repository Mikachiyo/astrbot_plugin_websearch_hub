"""聚合搜索中心 — 多搜索提供商轮询回退的统一 web_search 工具。"""

import json
import re
import time
from pathlib import Path

import httpx
from quart import jsonify, request

from astrbot.api import AstrBotConfig, logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.tool import FunctionTool


class ProviderSearchError(Exception):
    """搜索请求失败（网络错误、鉴权失败、限流等），可轮询下一个 key / 提供商。"""


def _norm_base(url: str) -> str:
    return (url or "").strip().rstrip("/")


async def _search_gemini(cfg: dict, key: str, query: str, client: httpx.AsyncClient) -> dict:
    base = _norm_base(cfg.get("base_url") or "https://generativelanguage.googleapis.com")
    model = (cfg.get("model") or "gemini-2.5-flash").strip()
    url = f"{base}/v1beta/models/{model}:generateContent"
    # 同时带两种鉴权头，官方走 x-goog-api-key，中转通常走 Bearer
    headers = {"x-goog-api-key": key, "Authorization": f"Bearer {key}"}
    body = {
        "contents": [{"role": "user", "parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],
    }
    resp = await client.post(url, headers=headers, json=body)
    if resp.status_code != 200:
        raise ProviderSearchError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    candidate = (data.get("candidates") or [{}])[0]
    parts = ((candidate.get("content") or {}).get("parts")) or []
    answer = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    results = []
    grounding = candidate.get("groundingMetadata") or {}
    for chunk in grounding.get("groundingChunks") or []:
        web = chunk.get("web") or {}
        if web.get("uri"):
            results.append({"title": web.get("title") or web["uri"], "url": web["uri"], "snippet": ""})
    return {"answer": answer, "results": results}


async def _search_grok(cfg: dict, key: str, query: str, client: httpx.AsyncClient) -> dict:
    base = _norm_base(cfg.get("base_url") or "https://api.x.ai/v1")
    model = (cfg.get("model") or "grok-4-fast").strip()
    url = f"{base}/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}
    body = {
        "model": model,
        "messages": [{"role": "user", "content": query}],
        "search_parameters": {"mode": "on", "return_citations": True},
    }
    resp = await client.post(url, headers=headers, json=body)
    if resp.status_code != 200:
        raise ProviderSearchError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    message = ((data.get("choices") or [{}])[0]).get("message") or {}
    answer = (message.get("content") or "").strip()
    citations = data.get("citations") or message.get("citations") or []
    results = [
        {"title": c, "url": c, "snippet": ""}
        for c in citations
        if isinstance(c, str)
    ]
    return {"answer": answer, "results": results}


async def _search_openai(cfg: dict, key: str, query: str, client: httpx.AsyncClient) -> dict:
    base = _norm_base(cfg.get("base_url") or "https://api.openai.com/v1")
    model = (cfg.get("model") or "gpt-5-mini").strip()
    url = f"{base}/responses"
    headers = {"Authorization": f"Bearer {key}"}
    body = {
        "model": model,
        "input": query,
        "tools": [{"type": "web_search"}],
        "store": False,
    }
    resp = await client.post(url, headers=headers, json=body)
    if resp.status_code != 200:
        raise ProviderSearchError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    answer_parts: list[str] = []
    results = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text":
                answer_parts.append(content.get("text", ""))
                for ann in content.get("annotations") or []:
                    if isinstance(ann, dict) and ann.get("type") == "url_citation" and ann.get("url"):
                        results.append({
                            "title": ann.get("title") or ann["url"],
                            "url": ann["url"],
                            "snippet": "",
                        })
    answer = "".join(answer_parts).strip()
    if not results:
        _extract_markdown_links(answer, results)
    return {"answer": answer, "results": results}


_MD_LINK_RE = re.compile(r"\[([^\]]{1,120})\]\((https?://[^)\s]+)\)")


def _extract_markdown_links(text: str, results: list) -> None:
    """部分中转站（如把 Gemini grounding 包成 Responses 格式的站点）不返回
    url_citation annotations，而是把来源以 Markdown 链接写在正文里，这里兜底提取。"""
    seen = {r["url"] for r in results}
    for title, url in _MD_LINK_RE.findall(text or ""):
        if url not in seen:
            seen.add(url)
            results.append({"title": title.strip() or url, "url": url, "snippet": ""})


async def _search_tavily(cfg: dict, key: str, query: str, client: httpx.AsyncClient) -> dict:
    base = _norm_base(cfg.get("base_url") or "https://api.tavily.com")
    url = f"{base}/search"
    body = {"api_key": key, "query": query}
    max_results = int(cfg.get("max_results") or 0)
    if max_results > 0:
        body["max_results"] = max_results
    resp = await client.post(url, json=body)
    if resp.status_code != 200:
        raise ProviderSearchError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    results = [
        {
            "title": r.get("title") or r.get("url") or "",
            "url": r.get("url") or "",
            "snippet": (r.get("content") or "")[:300],
        }
        for r in data.get("results") or []
        if isinstance(r, dict) and r.get("url")
    ]
    return {"answer": None, "results": results}


_SEARCHERS = {
    "gemini_native": _search_gemini,
    "openai_chat_search": _search_grok,
    "openai_responses": _search_openai,
    "tavily": _search_tavily,
}


# ---------- AstrBot 原生网页搜索配置（cmd_config.json provider_settings） ----------

_NATIVE_KEY_FIELD = {
    "tavily": "websearch_tavily_key",
    "bocha": "websearch_bocha_key",
    "brave": "websearch_brave_key",
    "firecrawl": "websearch_firecrawl_key",
    "baidu_ai_search": "websearch_baidu_app_builder_key",
}


def _load_astrbot_native_websearch() -> tuple[str, list[str]]:
    """读取 AstrBot 主配置里的网页搜索服务与 Key。"""
    cfg_path = Path(get_astrbot_data_path()) / "cmd_config.json"
    with open(cfg_path, encoding="utf-8-sig") as f:
        data = json.load(f)
    ps = data.get("provider_settings", {}) or {}
    provider = ps.get("websearch_provider", "tavily")
    keys = ps.get(_NATIVE_KEY_FIELD.get(provider, ""), [])
    if isinstance(keys, str):
        keys = [keys] if keys else []
    return provider, [k for k in keys if isinstance(k, str) and k.strip()]


async def _search_astrbot_native(cfg: dict, key: str, query: str, client: httpx.AsyncClient) -> dict:
    provider = cfg.get("_native_provider", "tavily")
    max_results = int(cfg.get("max_results") or 0)
    if provider == "tavily":
        return await _search_tavily(cfg, key, query, client)

    if provider == "bocha":
        body = {"query": query, "count": max_results or 10}
        resp = await client.post(
            "https://api.bochaai.com/v1/web-search",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
        )
        if resp.status_code != 200:
            raise ProviderSearchError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        rows = ((resp.json().get("data") or {}).get("webPages") or {}).get("value") or []
        results = [
            {"title": r.get("name") or r.get("url") or "", "url": r.get("url") or "", "snippet": (r.get("snippet") or "")[:300]}
            for r in rows if isinstance(r, dict) and r.get("url")
        ]
        return {"answer": None, "results": results}

    if provider == "brave":
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results or 10},
            headers={"Accept": "application/json", "X-Subscription-Token": key},
        )
        if resp.status_code != 200:
            raise ProviderSearchError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        rows = (resp.json().get("web") or {}).get("results") or []
        results = [
            {"title": r.get("title") or r.get("url") or "", "url": r.get("url") or "", "snippet": (r.get("description") or "")[:300]}
            for r in rows if isinstance(r, dict) and r.get("url")
        ]
        return {"answer": None, "results": results}

    if provider == "firecrawl":
        body = {"query": query}
        if max_results:
            body["limit"] = max_results
        resp = await client.post(
            "https://api.firecrawl.dev/v2/search",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
        )
        if resp.status_code != 200:
            raise ProviderSearchError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        rows = resp.json().get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("web") or []
        results = [
            {"title": r.get("title") or r.get("url") or "", "url": r.get("url") or "",
             "snippet": ((r.get("description") or r.get("snippet") or "")[:300])}
            for r in rows if isinstance(r, dict) and r.get("url")
        ]
        return {"answer": None, "results": results}

    if provider == "baidu_ai_search":
        body = {"messages": [{"role": "user", "content": query}]}
        resp = await client.post(
            "https://qianfan.baidubce.com/v2/ai_search/web_search",
            json=body,
            headers={
                "Authorization": f"Bearer {key}",
                "X-Appbuilder-Authorization": f"Bearer {key}",
            },
        )
        if resp.status_code != 200:
            raise ProviderSearchError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        rows = resp.json().get("references") or []
        results = [
            {"title": r.get("title") or r.get("url") or "", "url": r.get("url") or "", "snippet": (r.get("content") or "")[:300]}
            for r in rows if isinstance(r, dict) and r.get("url")
        ]
        return {"answer": None, "results": results}

    raise ProviderSearchError(f"未知的 AstrBot 原生搜索服务: {provider}")


_SEARCHERS = {
    "gemini_native": _search_gemini,
    "openai_chat_search": _search_grok,
    "openai_responses": _search_openai,
    "tavily": _search_tavily,
    "astrbot_native": _search_astrbot_native,
}

# 模板未显式选择协议时，按模板预设推断
_TEMPLATE_DEFAULT_PROTOCOL = {
    "gemini_search": "gemini_native",
    "grok_search": "openai_chat_search",
    "openai_search": "openai_responses",
    "tavily_search": "tavily",
    "custom_search": "openai_responses",
    "astrbot_search": "openai_responses",
    "astrbot_native_search": "astrbot_native",
}


# AstrBot 提供商适配器类型 -> 搜索协议
_PROTOCOL_FROM_PROVIDER_TYPE = {
    "openai_responses": "openai_responses",
    "googlegenai_chat_completion": "gemini_native",
    "openai_chat_completion": "openai_chat_search",
}


class SearchHub:
    """提供商与 Key 的轮询调度。"""

    def __init__(self, config: AstrBotConfig, context=None):
        self._config = config
        self._context = context
        self._rr_index: dict[int, int] = {}  # 每个提供商的 key 轮询游标
        self._key_fail_until: dict[str, float] = {}  # key -> 冷却截止时间

    def _resolve_entry(self, provider: dict) -> dict:
        """合并 AstrBot 已配置提供商的地址/Key/模型；protocol=auto 时推断协议。"""
        eff = dict(provider)
        pid = (provider.get("astr_provider_id") or "").strip()
        inst = None
        if pid and self._context is not None:
            try:
                inst = self._context.get_provider_by_id(pid)
            except Exception as e:
                logger.warning(f"[websearch_hub] 获取 AstrBot 提供商 {pid} 失败: {e}")
        if inst is not None:
            pcfg = getattr(inst, "provider_config", {}) or {}
            if pcfg.get("api_base"):
                eff["base_url"] = pcfg["api_base"]
            keys = pcfg.get("key")
            if isinstance(keys, str):
                keys = [keys]
            if keys:
                eff["api_keys"] = keys
            try:
                model = inst.get_model()
            except Exception:
                model = None
            if model:
                eff["model"] = model
            eff["_astr_type"] = pcfg.get("type", "")
        protocol = provider.get("protocol") or "auto"
        if protocol == "auto":
            if eff.get("_astr_type") in _PROTOCOL_FROM_PROVIDER_TYPE:
                protocol = _PROTOCOL_FROM_PROVIDER_TYPE[eff["_astr_type"]]
            else:
                protocol = _TEMPLATE_DEFAULT_PROTOCOL.get(
                    provider.get("__template_key", ""), ""
                )
        eff["_protocol"] = protocol
        if protocol == "astrbot_native":
            try:
                native_provider, native_keys = _load_astrbot_native_websearch()
                eff["_native_provider"] = native_provider
                eff["api_keys"] = native_keys
            except Exception as e:
                logger.warning(f"[websearch_hub] 读取 AstrBot 原生搜索配置失败: {e}")
                eff["api_keys"] = []
        return eff

    def _cooldown(self) -> float:
        return float(self._config.get("key_cooldown_seconds", 300))

    def _available_keys(self, provider: dict, idx: int) -> list[str]:
        keys = [k.strip() for k in (provider.get("api_keys") or []) if isinstance(k, str) and k.strip()]
        now = time.time()
        usable = [k for k in keys if self._key_fail_until.get(k, 0) <= now]
        if not usable:
            usable = keys  # 全部冷却中也照试，避免整体不可用
        if not usable:
            return []
        start = self._rr_index.get(idx, 0) % len(usable)
        rotated = usable[start:] + usable[:start]
        self._rr_index[idx] = start + 1
        return rotated

    def _mark_failed(self, key: str) -> None:
        self._key_fail_until[key] = time.time() + self._cooldown()

    def _mark_ok(self, key: str) -> None:
        self._key_fail_until.pop(key, None)

    async def search(self, query: str) -> tuple[dict, str] | tuple[None, str]:
        """按配置顺序轮询提供商。返回 (结果, 提供商名) 或 (None, 错误汇总)。"""
        providers = self._config.get("providers") or []
        errors: list[str] = []
        for idx, provider in enumerate(providers):
            if not isinstance(provider, dict) or not provider.get("enabled", True):
                continue
            eff = self._resolve_entry(provider)
            searcher = _SEARCHERS.get(eff.get("_protocol", ""))
            if not searcher:
                continue
            name = provider.get("name") or provider.get("__template_key")
            keys = self._available_keys(eff, idx)
            if not keys:
                errors.append(f"{name}: 未配置 API Key")
                continue
            timeout = float(eff.get("timeout") or 60)
            for key in keys:
                try:
                    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                        result = await searcher(eff, key, query, client)
                    self._mark_ok(key)
                    max_results = int(eff.get("max_results") or 0)
                    if max_results > 0:
                        result["results"] = result["results"][:max_results]
                    return result, name
                except Exception as e:
                    self._mark_failed(key)
                    errors.append(f"{name}(key...{key[-4:]}): {e}")
                    logger.warning(f"[websearch_hub] {name} key 失败: {e}")
        return None, "; ".join(errors) or "没有可用的搜索提供商"

    async def test_entry(self, provider: dict, query: str = "hello world") -> tuple[bool, str]:
        """用第一个可用 key 单测一个提供商条目，返回 (是否成功, 详情)。"""
        eff = self._resolve_entry(provider)
        protocol = eff.get("_protocol", "")
        searcher = _SEARCHERS.get(protocol)
        if not searcher:
            return False, f"无法确定请求协议（protocol={protocol!r}）"
        keys = self._available_keys(eff, 0)
        if not keys:
            return False, "未配置 API Key（也未选择 AstrBot 提供商）"
        timeout = float(eff.get("timeout") or 60)
        last_err = None
        for key in keys:
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    result = await searcher(eff, key, query, client)
                self._mark_ok(key)
                n = len(result.get("results") or [])
                answer_len = len(result.get("answer") or "")
                return True, (
                    f"协议={protocol}，模型={eff.get('model') or '(无)'}，"
                    f"回答 {answer_len} 字符，来源 {n} 条"
                )
            except Exception as e:
                self._mark_failed(key)
                last_err = f"key...{key[-4:]}: {e}"
        return False, f"所有 Key 均失败，最后一次错误：{last_err}"


def _format_result(data: dict, provider_name: str, query: str) -> str:
    lines = [f"搜索(query={query}, 来源={provider_name})结果："]
    if data.get("answer"):
        lines.append(f"\n【摘要回答】\n{data['answer']}")
    if data.get("results"):
        lines.append("\n【来源列表】")
        for i, r in enumerate(data["results"], 1):
            line = f"{i}. {r['title']}\n   {r['url']}"
            if r.get("snippet"):
                line += f"\n   {r['snippet']}"
            lines.append(line)
    if not data.get("answer") and not data.get("results"):
        lines.append("（该搜索源返回了空结果，未更换搜索源。）")
    return "\n".join(lines)


class WebSearchTool(FunctionTool):
    def __init__(self, hub: SearchHub):
        super().__init__(
            name="web_search",
            description=(
                "联网搜索。当需要查询最新资讯、实时信息、事实核查或你不确定的内容时调用。"
                "返回摘要回答和来源列表；如需读取某个网页全文，请再调用 fetch_web_page。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词或问题"}
                },
                "required": ["query"],
            },
        )
        object.__setattr__(self, "_hub", hub)

    async def call(self, context, **kwargs) -> str:
        query = (kwargs.get("query") or "").strip()
        if not query:
            return "error: query 不能为空"
        data, info = await self._hub.search(query)
        if data is None:
            return f"所有搜索源均失败：{info}"
        return _format_result(data, info, query)


class FetchWebPageTool(FunctionTool):
    def __init__(self, max_chars: int):
        super().__init__(
            name="fetch_web_page",
            description="抓取指定网页的正文文本内容。在 web_search 返回来源后，需要阅读某条结果全文时使用。",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要抓取的网页 URL"}
                },
                "required": ["url"],
            },
        )
        object.__setattr__(self, "_max_chars", max_chars)

    async def call(self, context, **kwargs) -> str:
        url = (kwargs.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return "error: url 必须是 http(s) 链接"
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (AstrBot websearch_hub)"})
                resp.raise_for_status()
        except Exception as e:
            return f"error: 抓取失败: {e}"
        html = resp.text
        html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        max_chars = getattr(self, "_max_chars", 0)
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars] + "...(已截断)"
        return f"网页({url})正文：\n{text}"


@register(
    name="astrbot_plugin_websearch_hub",
    desc="聚合搜索中心：Gemini/Grok/GPT/Tavily 多搜索源轮询回退，带 dashboard 管理页",
    version="v1.1.0",
    author="Mikachiyo",
)
class WebSearchHubPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.hub = SearchHub(config, context)
        context.add_llm_tools(
            WebSearchTool(self.hub),
            FetchWebPageTool(int(config.get("fetch_max_chars", 0) or 0)),
        )
        self._register_web_apis()
        logger.info(
            f"[websearch_hub] 已就绪，搜索源数量: {len(config.get('providers') or [])}"
        )

    # ---------- Web API（供 pages/admin 管理页使用） ----------

    def _register_web_apis(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[websearch_hub] 当前 AstrBot 版本不支持 register_web_api，跳过注册")
            return
        register = self.context.register_web_api
        prefix = "/astrbot_plugin_websearch_hub"
        register(f"{prefix}/config", self.api_get_config, ["GET"], "获取搜索中心配置")
        register(f"{prefix}/config/save", self.api_save_config, ["POST"], "保存搜索中心配置")
        register(f"{prefix}/astr_providers", self.api_list_astr_providers, ["GET"], "列出 AstrBot 已配置的对话模型提供商")
        register(f"{prefix}/native_search", self.api_native_search_info, ["GET"], "获取 AstrBot 原生网页搜索服务配置")
        register(f"{prefix}/test", self.api_test_entry, ["POST"], "测试单个搜索源是否可用")

    async def api_get_config(self):
        try:
            return jsonify({
                "success": True,
                "providers": self.config.get("providers") or [],
                "key_cooldown_seconds": self.config.get("key_cooldown_seconds", 300),
                "fetch_max_chars": self.config.get("fetch_max_chars", 0),
            })
        except Exception as exc:
            logger.error(f"[websearch_hub] api_get_config 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_save_config(self):
        try:
            data = await request.get_json() or {}
            providers = data.get("providers")
            if not isinstance(providers, list):
                return jsonify({"success": False, "error": "providers 必须是列表"}), 400
            cleaned = []
            for item in providers:
                if isinstance(item, dict):
                    cleaned.append(self._sanitize_entry(item))
            self.config["providers"] = cleaned
            if "key_cooldown_seconds" in data:
                self.config["key_cooldown_seconds"] = int(data["key_cooldown_seconds"] or 300)
            if "fetch_max_chars" in data:
                self.config["fetch_max_chars"] = int(data["fetch_max_chars"] or 0)
            self.config.save_config()
            return jsonify({"success": True, "message": "保存成功"})
        except Exception as exc:
            logger.error(f"[websearch_hub] api_save_config 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    @staticmethod
    def _sanitize_entry(item: dict) -> dict:
        return {
            "__template_key": str(item.get("__template_key") or "custom_search"),
            "name": str(item.get("name") or "").strip() or "未命名",
            "enabled": bool(item.get("enabled", True)),
            "astr_provider_id": str(item.get("astr_provider_id") or "").strip(),
            "protocol": str(item.get("protocol") or "auto"),
            "base_url": str(item.get("base_url") or "").strip(),
            "model": str(item.get("model") or "").strip(),
            "api_keys": [str(k).strip() for k in (item.get("api_keys") or []) if str(k).strip()],
            "timeout": int(item.get("timeout") or 60),
            "max_results": int(item.get("max_results") or 0),
        }

    async def api_native_search_info(self):
        try:
            provider, keys = _load_astrbot_native_websearch()
            return jsonify({
                "success": True,
                "provider": provider,
                "key_count": len(keys),
                "configured": bool(keys),
            })
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_list_astr_providers(self):
        try:
            insts = getattr(self.context.provider_manager, "provider_insts", []) or []
            providers = []
            for inst in insts:
                try:
                    meta = inst.meta()
                    model = inst.get_model()
                except Exception:
                    continue
                pcfg = getattr(inst, "provider_config", {}) or {}
                providers.append({
                    "id": meta.id,
                    "type": meta.type,
                    "model": model or "",
                    "api_base": pcfg.get("api_base", ""),
                })
            return jsonify({"success": True, "providers": providers})
        except Exception as exc:
            logger.error(f"[websearch_hub] api_list_astr_providers 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_test_entry(self):
        try:
            data = await request.get_json() or {}
            entry = data.get("entry")
            if not isinstance(entry, dict):
                return jsonify({"success": False, "error": "entry 缺失"}), 400
            query = str(data.get("query") or "latest news").strip()
            ok, detail = await self.hub.test_entry(self._sanitize_entry(entry), query)
            return jsonify({"success": ok, "detail": detail})
        except Exception as exc:
            logger.error(f"[websearch_hub] api_test_entry 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500
