"""Starseeker Search plugin for Nekro Agent.

The plugin intentionally uses only Python's standard library so it can run in
minimal Nekro Agent containers without installing dynamic dependencies.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Annotated, Any

from nekro_agent.api.plugin import (
    Arg,
    CmdCtl,
    CommandExecutionContext,
    CommandPermission,
    CommandResponse,
    ConfigBase,
    NekroPlugin,
    SandboxMethodType,
)
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.core import logger
from pydantic import Field


plugin = NekroPlugin(
    name="星巡搜索",
    module_name="nekro_plugin_starseeker_search",
    description="像巡航星图一样为 Agent 探索互联网，支持文字搜索与以图搜图。",
    version="0.2.5",
    author="Akiyo_Codex",
    url="https://github.com/luoxiQAQ/nekro-plugin-starseeker-search",
)


@plugin.mount_config()
class StarseekerSearchConfig(ConfigBase):
    """星巡搜索配置"""

    PROVIDER: str = Field(
        default="auto",
        title="搜索服务",
        description="auto / brave / tavily / searxng / duckduckgo / bing / fallback。auto 会按可用配置依次尝试。",
    )
    BRAVE_API_KEY: str = Field(
        default="",
        title="Brave Search API Key",
        description="Brave Search API 的订阅密钥。",
    )
    TAVILY_API_KEY: str = Field(
        default="",
        title="Tavily API Key",
        description="Tavily API 密钥，适合 Agent 搜索摘要。",
    )
    SEARXNG_BASE_URL: str = Field(
        default="",
        title="SearXNG 地址",
        description="自建 SearXNG 地址，例如 https://search.example.com。",
    )
    MAX_RESULTS: int = Field(default=5, title="默认结果数", ge=1, le=10)
    TIMEOUT_SECONDS: int = Field(default=12, title="请求超时秒数", ge=3, le=60)
    ALLOW_BING_FALLBACK: bool = Field(
        default=True,
        title="允许无 Key 兜底",
        description="没有正式 API 配置或正式 API 失败时，允许使用无 Key 搜索源兜底。",
    )
    # ---------- 以图搜图配置 ----------
    IMAGE_SEARCH_PROVIDER: str = Field(
        default="auto",
        title="图片搜索服务",
        description="auto / saucenao / iqdb / tracemoe。auto 按可用配置依次尝试。",
    )
    SAUCENAO_API_KEY: str = Field(
        default="",
        title="SauceNAO API Key",
        description="SauceNAO 搜图 API Key，https://saucenao.com/user.php 注册获取（免费 200 次/天）。",
    )
    IMAGE_SEARCH_MIN_SIMILARITY: float = Field(
        default=55.0,
        title="搜图最低相似度",
        description="低于此相似度的结果将被过滤（0-100），仅对提供相似度的引擎生效。",
        ge=0.0,
        le=100.0,
    )
    IMAGE_SEARCH_MAX_RESULTS: int = Field(
        default=3,
        title="搜图默认结果数",
        ge=1,
        le=10,
    )


config: StarseekerSearchConfig = plugin.get_config(StarseekerSearchConfig)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str


class SearchError(RuntimeError):
    pass


class SearchProviderAuthError(SearchError):
    pass


FORMAL_PROVIDERS = {"brave", "tavily", "searxng"}
NO_KEY_PROVIDERS = {"duckduckgo", "bing", "fallback"}


LOW_VALUE_DOMAINS = (
    "support.google.com",
    "accounts.google.com",
    "youtube.com",
    "google.com/search",
    "baike.baidu.com",
)

QUERY_STOPWORDS = {
    "联网",
    "搜索",
    "查询",
    "当前",
    "一下",
    "关于",
    "相关",
    "信息",
    "消息",
    "官方",
    "有没有",
    "是否",
    "什么时候",
    "the",
    "and",
    "for",
    "from",
    "with",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "how",
    "is",
    "are",
    "was",
    "were",
    "this",
    "that",
    "today",
    "latest",
}


def _clean_text(value: Any, limit: int = 500) -> str:
    text = "" if value is None else str(value)
    text = html.unescape(text)
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "..."
    return text


def _http_text(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 12,
) -> str:
    data = None
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    }
    if headers:
        request_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        message = f"HTTP {exc.code}: {body}"
        if exc.code in {401, 403}:
            raise SearchProviderAuthError(message) from exc
        raise SearchError(message) from exc
    except Exception as exc:
        raise SearchError(f"{type(exc).__name__}: {exc}") from exc


def _http_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
    text = _http_text(*args, **kwargs)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SearchError(f"Invalid JSON response: {text[:200]}") from exc


def _decode_redirect_url(url: str) -> str:
    url = html.unescape(url)
    if url.startswith("//"):
        url = "https:" + url
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return query["uddg"][0]
    if "u" in query and query["u"]:
        return query["u"][0]
    return url


def _important_terms(query: str) -> list[str]:
    raw_parts = re.split(r'[\s,.;:!?"\'`~@#$%^&*+=|/\\<>()\[\]{}-]+', query.lower())
    terms = []
    for raw_part in raw_parts:
        part = raw_part.strip()
        if len(part) < 2 or part in QUERY_STOPWORDS:
            continue
        terms.append(part)
        sub_parts = re.findall(r"[a-z]+[a-z0-9._+-]*|\d+[a-z0-9._+-]*|[\u4e00-\u9fff]+", part)
        if len(sub_parts) > 1:
            terms.extend(sub for sub in sub_parts if len(sub) >= 2 and sub not in QUERY_STOPWORDS)
        if re.fullmatch(r"[\u4e00-\u9fff]{5,}", part):
            for size in (2, 3, 4):
                for index in range(0, len(part) - size + 1):
                    gram = part[index : index + size]
                    if gram not in QUERY_STOPWORDS:
                        terms.append(gram)
    if terms:
        return list(dict.fromkeys(terms))[:12]

    segments = re.findall(r"[a-z]+[a-z0-9._+-]*|\d+[a-z0-9._+-]*|[\u4e00-\u9fff]+", query.lower())
    for segment in segments:
        if segment in QUERY_STOPWORDS:
            continue
        if len(segment) <= 4:
            terms.append(segment)
        elif re.fullmatch(r"[\u4e00-\u9fff]+", segment):
            for size in (2, 3, 4):
                for index in range(0, len(segment) - size + 1):
                    gram = segment[index : index + size]
                    if gram not in QUERY_STOPWORDS:
                        terms.append(gram)
        else:
            terms.append(segment)
    return list(dict.fromkeys(terms))[:12]

def _query_variants(query: str) -> list[str]:
    query = re.sub(r"\s+", " ", query).strip()
    terms = _important_terms(query)
    variants = []
    if len(terms) >= 2:
        variants.append(" ".join(f'"{term}"' for term in terms[:4]))
    variants.append(query)
    compact = re.sub(r"\s+", "", query)
    if compact and compact != query:
        variants.append(compact)
    if len(terms) >= 2:
        variants.append(" ".join(terms[:4]) + " 新闻")
        variants.append(" ".join(terms[:4]) + " 讨论")
    return list(dict.fromkeys(variant for variant in variants if variant))[:5]


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _is_low_confidence_no_key_result(query: str, results: list[SearchResult]) -> bool:
    return bool(results) and not _contains_cjk(query) and all(item.source in {"Bing HTML", "Bing RSS"} for item in results)


def _score_result(query: str, item: SearchResult) -> float:
    terms = _important_terms(query)
    title = item.title.lower()
    snippet = item.snippet.lower()
    url = item.url.lower()
    haystack = f"{title} {snippet} {url}"
    score = 0.0
    for term in terms:
        if term in title:
            score += 4.0
        elif term in snippet:
            score += 2.5
        elif term in url:
            score += 1.0
    if terms:
        matched = sum(1 for term in terms if term in haystack)
        score += 5.0 * matched / len(terms)
        if matched == len(terms):
            score += 4.0
    if any(domain in url for domain in LOW_VALUE_DOMAINS):
        score -= 5.0
    if not item.snippet:
        score -= 1.0
    return score


def _rank_and_filter_results(query: str, candidates: list[SearchResult], limit: int) -> list[SearchResult]:
    unique = _normalize_results(candidates, len(candidates))
    scored = sorted(
        ((item, _score_result(query, item)) for item in unique),
        key=lambda pair: pair[1],
        reverse=True,
    )
    if not scored:
        return []

    terms = _important_terms(query)
    threshold = 5.0 if len(terms) >= 2 else 2.0
    min_matches = 2 if len(terms) >= 3 else 1
    filtered = [
        item
        for item, score in scored
        if score >= threshold and sum(1 for term in terms if term in f"{item.title} {item.snippet} {item.url}".lower()) >= min_matches
    ]
    if not filtered and scored[0][1] >= threshold - 1.5:
        filtered = [scored[0][0]]
    return filtered[:limit]


def _normalize_results(candidates: list[SearchResult], limit: int) -> list[SearchResult]:
    seen = set()
    unique = []
    for item in candidates:
        clean_url = item.url.split("#", 1)[0]
        if not clean_url.startswith("http") or clean_url in seen:
            continue
        seen.add(clean_url)
        unique.append(SearchResult(item.title, clean_url, item.snippet, item.source))
        if len(unique) >= limit:
            break
    return unique


def _search_brave(query: str, limit: int, timeout: int) -> list[SearchResult]:
    if not config.BRAVE_API_KEY.strip():
        raise SearchError("Brave API key is not configured")
    params = urllib.parse.urlencode(
        {
            "q": query,
            "count": min(limit, 10),
            "search_lang": "zh-hans",
            "country": "CN",
            "safesearch": "moderate",
            "text_decorations": "false",
        }
    )
    data = _http_json(
        f"https://api.search.brave.com/res/v1/web/search?{params}",
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": config.BRAVE_API_KEY.strip(),
        },
        timeout=timeout,
    )
    results = []
    for item in data.get("web", {}).get("results", [])[:limit]:
        results.append(
            SearchResult(
                title=_clean_text(item.get("title"), 160),
                url=str(item.get("url") or ""),
                snippet=_clean_text(item.get("description"), 360),
                source="Brave",
            )
        )
    return [item for item in results if item.url]


def _search_tavily(query: str, limit: int, timeout: int) -> list[SearchResult]:
    if not config.TAVILY_API_KEY.strip():
        raise SearchError("Tavily API key is not configured")
    payload = {
        "query": query,
        "max_results": min(limit, 10),
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
    }
    headers = {"Authorization": f"Bearer {config.TAVILY_API_KEY.strip()}"}
    try:
        data = _http_json(
            "https://api.tavily.com/search",
            method="POST",
            headers=headers,
            payload=payload,
            timeout=timeout,
        )
    except SearchProviderAuthError:
        raise
    except SearchError:
        payload_with_key = dict(payload)
        payload_with_key["api_key"] = config.TAVILY_API_KEY.strip()
        data = _http_json(
            "https://api.tavily.com/search",
            method="POST",
            payload=payload_with_key,
            timeout=timeout,
        )
    results = []
    for item in data.get("results", [])[:limit]:
        results.append(
            SearchResult(
                title=_clean_text(item.get("title"), 160),
                url=str(item.get("url") or ""),
                snippet=_clean_text(item.get("content"), 360),
                source="Tavily",
            )
        )
    return [item for item in results if item.url]


def _search_searxng(query: str, limit: int, timeout: int) -> list[SearchResult]:
    base_url = config.SEARXNG_BASE_URL.strip().rstrip("/")
    if not base_url:
        raise SearchError("SearXNG base URL is not configured")
    params = urllib.parse.urlencode(
        {
            "q": query,
            "format": "json",
            "language": "all",
            "categories": "general",
        }
    )
    data = _http_json(f"{base_url}/search?{params}", timeout=timeout)
    results = []
    for item in data.get("results", [])[:limit]:
        results.append(
            SearchResult(
                title=_clean_text(item.get("title"), 160),
                url=str(item.get("url") or ""),
                snippet=_clean_text(item.get("content"), 360),
                source="SearXNG",
            )
        )
    return [item for item in results if item.url]


def _search_duckduckgo_lite(query: str, limit: int, timeout: int) -> list[SearchResult]:
    params = urllib.parse.urlencode({"q": query})
    page = _http_text(f"https://lite.duckduckgo.com/lite/?{params}", timeout=timeout)
    anchors = list(
        re.finditer(
            r"<a\b(?=[^>]*class=['\"]result-link['\"])(?=[^>]*href=['\"]([^'\"]+)['\"])[^>]*>(.*?)</a>",
            page,
            flags=re.I | re.S,
        )
    )
    results = []
    for index, anchor in enumerate(anchors):
        href = _decode_redirect_url(anchor.group(1))
        block_end = anchors[index + 1].start() if index + 1 < len(anchors) else len(page)
        block = page[anchor.end() : block_end]
        snippet_match = re.search(
            r"<td\b[^>]*class=['\"]result-snippet['\"][^>]*>(.*?)</td>",
            block,
            flags=re.I | re.S,
        )
        title = _clean_text(anchor.group(2), 180)
        snippet = _clean_text(snippet_match.group(1) if snippet_match else "", 420)
        if title and href.startswith("http"):
            results.append(SearchResult(title=title, url=href, snippet=snippet, source="DuckDuckGo Lite"))
        if len(results) >= limit:
            break
    return results


def _search_360(query: str, limit: int, timeout: int) -> list[SearchResult]:
    params = urllib.parse.urlencode({"q": query})
    page = _http_text(
        f"https://www.so.com/s?{params}",
        headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"},
        timeout=timeout,
    )
    blocks = re.findall(
        r"<li\b[^>]*class=['\"][^'\"]*\bres-list\b[^'\"]*['\"][^>]*>(.*?)</li>",
        page,
        flags=re.I | re.S,
    )
    results = []
    for block in blocks:
        title_match = re.search(r"<h3\b[^>]*class=['\"][^'\"]*\bres-title\b[^'\"]*['\"][^>]*>(.*?)</h3>", block, flags=re.I | re.S)
        if not title_match:
            continue
        title_html = title_match.group(1)
        url_match = re.search(r"\bdata-mdurl=['\"]([^'\"]+)['\"]", title_html, flags=re.I)
        if not url_match:
            url_match = re.search(r"<a\b[^>]*href=['\"]([^'\"]+)['\"]", title_html, flags=re.I | re.S)
        url = _decode_redirect_url(url_match.group(1)) if url_match else ""
        desc_match = re.search(r"<p\b[^>]*class=['\"][^'\"]*\bres-desc\b[^'\"]*['\"][^>]*>(.*?)</p>", block, flags=re.I | re.S)
        if not desc_match:
            desc_match = re.search(r"<span\b[^>]*class=['\"][^'\"]*\bres-list-summary\b[^'\"]*['\"][^>]*>(.*?)</span>", block, flags=re.I | re.S)
        title = _clean_text(title_html, 180)
        snippet = _clean_text(desc_match.group(1) if desc_match else "", 420)
        if title and url.startswith("http"):
            results.append(SearchResult(title=title, url=url, snippet=snippet, source="360 Search"))
        if len(results) >= limit:
            break
    return results


def _search_bing_rss(query: str, limit: int, timeout: int) -> list[SearchResult]:
    if not config.ALLOW_BING_FALLBACK:
        raise SearchError("no-key fallback is disabled")
    params = urllib.parse.urlencode({"q": query, "format": "rss"})
    text = _http_text(f"https://www.bing.com/search?{params}", timeout=timeout)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SearchError(f"Invalid Bing RSS: {exc}") from exc
    results = []
    for item in root.findall("./channel/item")[:limit]:
        title = _clean_text(item.findtext("title"), 180)
        url = html.unescape(item.findtext("link") or "")
        snippet = _clean_text(item.findtext("description"), 420)
        if title and url.startswith("http"):
            results.append(SearchResult(title=title, url=url, snippet=snippet, source="Bing RSS"))
    return results


def _extract_bing_blocks(page: str) -> list[str]:
    blocks = re.findall(
        r'<li[^>]+class="[^"]*\bb_algo\b[^"]*"[^>]*>(.*?)</li>',
        page,
        flags=re.I | re.S,
    )
    if blocks:
        return blocks
    return re.findall(r"<h2[^>]*>.*?</h2>.*?(?:<p[^>]*>.*?</p>)?", page, flags=re.I | re.S)


def _search_bing_fallback(query: str, limit: int, timeout: int) -> list[SearchResult]:
    if not config.ALLOW_BING_FALLBACK:
        raise SearchError("no-key fallback is disabled")
    params = urllib.parse.urlencode({"q": query, "count": min(limit, 10)})
    page = _http_text(f"https://cn.bing.com/search?{params}", timeout=timeout)
    results = []
    for block in _extract_bing_blocks(page):
        link_match = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.I | re.S)
        if not link_match:
            continue
        url = html.unescape(link_match.group(1))
        if not url.startswith("http"):
            continue
        title = _clean_text(link_match.group(2), 160)
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, flags=re.I | re.S)
        snippet = _clean_text(snippet_match.group(1) if snippet_match else "", 360)
        if title and url:
            results.append(SearchResult(title=title, url=url, snippet=snippet, source="Bing HTML"))
        if len(results) >= limit:
            break
    return results


def _search_fallback(query: str, limit: int, timeout: int) -> list[SearchResult]:
    if not config.ALLOW_BING_FALLBACK:
        raise SearchError("no-key fallback is disabled")
    errors = []
    candidates = []
    if _contains_cjk(query):
        engine_plan = [
            ("bing_html", _search_bing_fallback, 4, min(timeout, 8)),
            ("bing_rss", _search_bing_rss, 2, min(timeout, 8)),
            ("duckduckgo", _search_duckduckgo_lite, 1, min(timeout, 6)),
            ("360", _search_360, 1, min(timeout, 8)),
        ]
    else:
        engine_plan = [
            ("duckduckgo", _search_duckduckgo_lite, 2, min(timeout, 6)),
            ("bing_html", _search_bing_fallback, 4, min(timeout, 8)),
            ("bing_rss", _search_bing_rss, 1, min(timeout, 8)),
        ]
    for engine_name, engine, variant_count, engine_timeout in engine_plan:
        for variant in _query_variants(query)[:variant_count]:
            try:
                candidates.extend(engine(variant, max(limit * 2, 8), engine_timeout))
                ranked = _rank_and_filter_results(query, candidates, limit)
                if len(ranked) >= min(limit, 3):
                    if _is_low_confidence_no_key_result(query, ranked):
                        raise SearchError(
                            "no-key fallback returned only low-confidence Bing results for a non-Chinese query; "
                            "configure Tavily, Brave, or SearXNG for reliable search"
                        )
                    return ranked
            except Exception as exc:
                errors.append(f"{engine_name}({variant}): {exc}")
                logger.warning(f"星巡搜索 fallback {engine_name} failed: {exc}")
    ranked = _rank_and_filter_results(query, candidates, limit)
    if ranked:
        if _is_low_confidence_no_key_result(query, ranked):
            raise SearchError(
                "no-key fallback returned only low-confidence Bing results for a non-Chinese query; "
                "configure Tavily, Brave, or SearXNG for reliable search"
            )
        return ranked
    raise SearchError("; ".join(errors[-4:]) or "fallback found no relevant results")


def _provider_order() -> list[str]:
    provider = config.PROVIDER.strip().lower()
    if provider and provider != "auto":
        return [provider]
    order = []
    if config.TAVILY_API_KEY.strip():
        order.append("tavily")
    if config.BRAVE_API_KEY.strip():
        order.append("brave")
    if config.SEARXNG_BASE_URL.strip():
        order.append("searxng")
    if config.ALLOW_BING_FALLBACK:
        order.append("fallback")
    return order


def _run_search(query: str, limit: int) -> tuple[list[SearchResult], list[str]]:
    timeout = max(3, min(int(config.TIMEOUT_SECONDS), 60))
    errors = []
    providers = _provider_order()
    if not providers:
        return [], ["no search provider configured; configure Tavily, Brave, SearXNG, or enable no-key fallback"]
    for index, provider in enumerate(providers):
        try:
            if provider in NO_KEY_PROVIDERS and not config.ALLOW_BING_FALLBACK:
                raise SearchError("no-key fallback is disabled")
            if provider == "brave":
                results = _search_brave(query, limit, timeout)
            elif provider == "tavily":
                results = _search_tavily(query, limit, timeout)
            elif provider == "searxng":
                results = _search_searxng(query, limit, timeout)
            elif provider == "duckduckgo":
                results = _rank_and_filter_results(query, _search_duckduckgo_lite(query, max(limit * 2, 8), timeout), limit)
            elif provider == "bing":
                bing_candidates = _search_bing_fallback(query, max(limit * 2, 8), timeout) + _search_bing_rss(query, max(limit * 2, 8), timeout)
                results = _rank_and_filter_results(query, bing_candidates, limit)
            elif provider == "fallback":
                results = _search_fallback(query, limit, timeout)
            else:
                raise SearchError(f"Unknown provider: {provider}")
            if provider in FORMAL_PROVIDERS:
                ranked_results = _normalize_results(results, limit)
            else:
                ranked_results = _rank_and_filter_results(query, results, limit)
            if ranked_results:
                return ranked_results, errors
            errors.append(f"{provider}: no relevant results")
        except SearchProviderAuthError as exc:
            errors.append(f"{provider}: authentication or permission failed: {exc}")
            logger.warning(f"星巡搜索 provider {provider} authentication failed: {exc}")
            has_later_formal_provider = any(item in FORMAL_PROVIDERS for item in providers[index + 1 :])
            if provider in FORMAL_PROVIDERS and not has_later_formal_provider:
                break
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
            logger.warning(f"星巡搜索 provider {provider} failed: {exc}")
    return [], errors


def _format_results(query: str, results: list[SearchResult], errors: list[str]) -> str:
    if not results:
        details = "\n".join(f"- {error}" for error in errors[-5:])
        return f"星巡搜索没有找到 `{query}` 的可用结果。\n{details}".strip()
    sources = "、".join(dict.fromkeys(item.source for item in results))
    lines = [f"星巡搜索结果：{query}", f"来源：{sources}"]
    if errors:
        lines.append("降级提示：" + "；".join(errors[-3:]))
    lines.append("")
    for index, item in enumerate(results, start=1):
        lines.append(f"{index}. {item.title}")
        if item.snippet:
            lines.append(f"   摘要：{item.snippet}")
        lines.append(f"   URL：{item.url}")
    return "\n".join(lines).strip()


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="web_search",
    description="联网搜索关键词、问题或网页主题，返回标题、摘要和来源链接。",
)
async def web_search(_ctx: AgentCtx, query: str, max_results: int | None = None) -> str:
    """联网搜索关键词、问题或网页主题，并返回可供回答引用的结果。

    Args:
        query: 要搜索的关键词、问题、新闻主题或网页主题。
        max_results: 返回结果数量，默认使用插件配置，范围 1-10。

    Returns:
        包含标题、摘要和 URL 的搜索结果。
    """
    query = (query or "").strip()
    if not query:
        return "星巡搜索需要一个非空查询。"
    limit = max_results if max_results is not None else config.MAX_RESULTS
    limit = max(1, min(int(limit), 10))
    loop = asyncio.get_running_loop()
    results, errors = await loop.run_in_executor(None, _run_search, query, limit)
    return _format_results(query, results, errors)


# ============================================================
# 以图搜图
# ============================================================


@dataclass
class ImageSearchResult:
    """单条图片搜索结果"""
    title: str
    url: str
    similarity: float       # 0-100, 0 表示引擎未提供
    author: str
    source_site: str
    extra_info: str
    engine: str


def _build_multipart_body(
    image_data: bytes,
    filename: str,
    fields: dict[str, str],
    file_field_name: str = "file",
) -> tuple[bytes, str]:
    """构建 multipart/form-data 请求体（纯标准库）"""
    boundary = "----StarSeekerImg" + uuid.uuid4().hex
    parts: list[bytes] = []

    for key, value in fields.items():
        parts.append(
            ("--" + boundary + "\r\n"
             "Content-Disposition: form-data; name=\"" + key + "\"\r\n\r\n"
             + value + "\r\n").encode("utf-8")
        )

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    }
    mime = mime_map.get(ext, "application/octet-stream")

    parts.append(
        ("--" + boundary + "\r\n"
         "Content-Disposition: form-data; name=\"" + file_field_name + "\"; "
         "filename=\"" + filename + "\"\r\n"
         "Content-Type: " + mime + "\r\n\r\n").encode("utf-8")
    )
    parts.append(image_data)
    parts.append(b"\r\n")
    parts.append(("--" + boundary + "--\r\n").encode("utf-8"))

    content_type = "multipart/form-data; boundary=" + boundary
    return b"".join(parts), content_type


# -------------------- SauceNAO --------------------

_SAUCENAO_SITE_KEYWORDS = {
    "pixiv": "Pixiv", "danbooru": "Danbooru", "gelbooru": "Gelbooru",
    "yande.re": "Yande.re", "konachan": "Konachan", "twitter": "Twitter/X",
    "deviantart": "DeviantArt", "artstation": "ArtStation", "nijie": "Nijie",
    "pawoo": "Pawoo", "seiga": "Niconico Seiga", "mangadex": "MangaDex",
    "e-hentai": "E-Hentai", "anime": "Anime", "anidb": "AniDB",
    "sankaku": "Sankaku", "bcy": "半次元",
}


def _parse_saucenao_index(index_name: str) -> str:
    name_lower = index_name.lower()
    for keyword, site in _SAUCENAO_SITE_KEYWORDS.items():
        if keyword in name_lower:
            return site
    return index_name.split(" - ")[0].strip() if " - " in index_name else "其他"


def _find_chromium(pw: Any) -> str:
    """定位可用的 Chromium：优先 Playwright 自带浏览器（受 PLAYWRIGHT_BROWSERS_PATH 影响），
    再退回系统常见安装路径。容器镜像更新或重建后浏览器位置可能变化，不能写死一个路径。"""
    candidates: list[str] = []
    try:
        candidates.append(pw.chromium.executable_path)
    except Exception:
        pass
    candidates += [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/opt/google/chrome/chrome",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    raise SearchError(
        "未找到 Chromium 浏览器（已尝试 Playwright 自带路径与系统常见路径），"
        "请运行 playwright install chromium 安装"
    )


def _search_saucenao(
    image_data: bytes,
    filename: str,
    limit: int,
    min_similarity: float,
    timeout: int,
) -> list[ImageSearchResult]:
    """SauceNAO 以图搜图（通过 Playwright 浏览器绕过 Cloudflare）"""
    import tempfile

    # 将图片写入临时文件供浏览器上传
    suffix = "." + (filename.rsplit(".", 1)[-1] if "." in filename else "jpg")
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(image_data)
        tmp.flush()
        tmp.close()
        tmp_path = tmp.name

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SearchError("SauceNAO 需要 playwright，请运行 pip install playwright && apt install chromium")

        with sync_playwright() as pw:
            chromium_path = _find_chromium(pw)
            browser = pw.chromium.launch(
                executable_path=chromium_path,
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            try:
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.6099.224 Safari/537.36"
                    ),
                    locale="zh-CN",
                )
                page = ctx.new_page()
                page.set_default_timeout(timeout * 1000)

                # 访问 SauceNAO 首页（过 Cloudflare）
                page.goto("https://saucenao.com/", timeout=timeout * 1000)
                page.wait_for_timeout(2000)

                title = page.title().lower()
                if "just a moment" in title or "challenge" in title:
                    page.wait_for_timeout(8000)
                    title = page.title().lower()
                    if "just a moment" in title:
                        raise SearchError("SauceNAO Cloudflare 验证未通过")

                # 上传图片
                file_inputs = page.query_selector_all('input[type="file"]')
                if not file_inputs:
                    raise SearchError("SauceNAO 页面未找到上传控件")

                file_inputs[0].set_input_files(tmp_path)
                submit = page.query_selector('input[type="submit"]')
                if not submit:
                    raise SearchError("SauceNAO 页面未找到提交按钮")

                submit.click()
                page.wait_for_load_state("domcontentloaded", timeout=timeout * 1000)
                page.wait_for_timeout(3000)

                content = page.content()
            finally:
                browser.close()

        # ---- 解析 HTML 结果 ----
        results: list[ImageSearchResult] = []

        # 提取每个结果块的相似度
        sims = re.findall(
            r'<div class="resultsimilarityinfo">(\d+\.\d+)%</div>',
            content,
        )
        # 提取每个结果的内容块
        blocks = re.findall(
            r'<td class="resulttablecontent">(.*?)</td>',
            content,
            flags=re.S,
        )

        for idx, block in enumerate(blocks):
            similarity = float(sims[idx]) if idx < len(sims) else 0.0
            if similarity < min_similarity:
                continue

            # 标题
            title_m = re.search(
                r'<div class="resulttitle"[^>]*>(.*?)</div>', block, re.S
            )
            title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ""

            # 内容列（作者、来源等）
            col_m = re.search(
                r'<div class="resultcontentcolumn">(.*?)</div>', block, re.S
            )
            col_text = ""
            if col_m:
                col_text = re.sub(r'<[^>]+>', ' ', col_m.group(1)).strip()
                col_text = re.sub(r'\s+', ' ', col_text)

            # 作者提取
            author = ""
            author_patterns = [
                r'(?:Member|Author|Creator|Artist)[:\s]+([^\n<]+)',
                r'(?:画师|作者)[:\s]+([^\n<]+)',
            ]
            for ap in author_patterns:
                am = re.search(ap, col_text, re.I)
                if am:
                    author = am.group(1).strip()
                    break

            # 来源链接（排除 saucenao 自身链接）
            links = re.findall(
                r'href="(https?://(?!saucenao)[^"]+)"', block
            )
            source_url = ""
            source_site = "其他"
            for link in links:
                link_clean = link.replace("&amp;", "&")
                link_lower = link_clean.lower()
                for kw, site in _SAUCENAO_SITE_KEYWORDS.items():
                    if kw in link_lower:
                        source_site = site
                        source_url = link_clean
                        break
                if source_url:
                    break

            if not source_url and links:
                source_url = links[0].replace("&amp;", "&")

            results.append(ImageSearchResult(
                title=title or "(无标题)",
                url=source_url,
                similarity=similarity,
                author=author or "(未知作者)",
                source_site=source_site,
                extra_info="",
                engine="SauceNAO",
            ))

        results.sort(key=lambda r: r.similarity, reverse=True)
        return results[:limit]

    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# -------------------- IQDB --------------------

def _search_iqdb(
    image_data: bytes,
    filename: str,
    limit: int,
    min_similarity: float,
    timeout: int,
) -> list[ImageSearchResult]:
    """IQDB 以图搜图（多 booru 站聚合）"""
    body, content_type = _build_multipart_body(image_data, filename, {})

    request = urllib.request.Request(
        "https://iqdb.org/",
        data=body,
        headers={
            "Content-Type": content_type,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Referer": "https://iqdb.org/",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            page = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise SearchError("IQDB 请求失败: " + str(exc)) from exc

    if "Just a moment" in page:
        raise SearchError("IQDB 被 Cloudflare 拦截")

    # IQDB 结果结构：每个匹配在 <table> 中，有相似度百分比和来源链接
    # 结果块之间用 <div> 或 <table> 分隔
    results: list[ImageSearchResult] = []

    # 找到所有包含相似度的结果块
    # IQDB 格式: "<td>NN% similarity</td>" 和来源链接
    result_tables = re.findall(
        r'<table[^>]*>(.*?)</table>',
        page,
        flags=re.S,
    )

    for table in result_tables:
        sim_match = re.search(r'(\d+)% similarity', table)
        if not sim_match:
            continue

        similarity = float(sim_match.group(1))
        if similarity < min_similarity:
            continue

        # 提取来源链接（各 booru 站）
        link_matches = re.findall(
            r'<a[^>]+href="(https?://[^"]+)"[^>]*>',
            table,
            flags=re.I,
        )

        source_url = ""
        source_site = "其他"
        for link in link_matches:
            if any(d in link.lower() for d in (
                "danbooru", "yande.re", "gelbooru", "konachan",
                "anime-pictures", "e-shuushuu", "zerochan",
                "sankaku",
            )):
                source_url = link
                break

        if not source_url and link_matches:
            source_url = link_matches[0]

        # 判断来源站点
        url_lower = source_url.lower()
        if "danbooru" in url_lower:
            source_site = "Danbooru"
        elif "yande.re" in url_lower:
            source_site = "Yande.re"
        elif "gelbooru" in url_lower:
            source_site = "Gelbooru"
        elif "konachan" in url_lower:
            source_site = "Konachan"
        elif "anime-pictures" in url_lower:
            source_site = "Anime-Pictures"
        elif "e-shuushuu" in url_lower:
            source_site = "E-Shuushuu"
        elif "zerochan" in url_lower:
            source_site = "Zerochan"
        elif "sankaku" in url_lower:
            source_site = "Sankaku"

        # 提取尺寸/分辨率信息
        res_match = re.search(r'(\d+)[×x](\d+)', table)
        extra = ""
        if res_match:
            extra = res_match.group(1) + "×" + res_match.group(2)

        # 标题（IQDB 通常没有标题，用来源站名代替）
        title = source_site + " #" + (source_url.rsplit("/", 1)[-1][:20] if "/" in source_url else "?")

        results.append(ImageSearchResult(
            title=title,
            url=source_url,
            similarity=similarity,
            author="",
            source_site=source_site,
            extra_info=extra,
            engine="IQDB",
        ))

        if len(results) >= limit:
            break

    results.sort(key=lambda r: r.similarity, reverse=True)
    return results[:limit]


# -------------------- trace.moe --------------------

def _search_tracemoe(
    image_data: bytes,
    filename: str,
    limit: int,
    min_similarity: float,
    timeout: int,
) -> list[ImageSearchResult]:
    """trace.moe 动画截图识别"""

    # 使用 multipart 上传（比 base64 支持更大的文件）
    boundary = "----TraceMoe" + uuid.uuid4().hex
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp",
    }
    mime = mime_map.get(ext, "image/jpeg")

    body = b""
    body += ("--" + boundary + "\r\n").encode()
    body += ("Content-Disposition: form-data; name=\"image\"; "
             "filename=\"" + filename + "\"\r\n").encode()
    body += ("Content-Type: " + mime + "\r\n\r\n").encode()
    body += image_data
    body += ("\r\n--" + boundary + "--\r\n").encode()

    request = urllib.request.Request(
        "https://api.trace.moe/search?anilistInfo&cutBorders",
        data=body,
        headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "User-Agent": "StarSeekerSearch/0.2",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 413:
            raise SearchError("trace.moe: 图片文件过大，请使用较小的图片") from exc
        raise SearchError("trace.moe HTTP " + str(exc.code)) from exc
    except Exception as exc:
        raise SearchError("trace.moe 请求失败: " + str(exc)) from exc

    results: list[ImageSearchResult] = []
    for item in data.get("result", [])[:limit * 2]:
        similarity = round(item.get("similarity", 0) * 100, 1)
        # trace.moe 对非动画截图会返回大量 70-80% 的近似噪音（多部无关番剧同时命中），
        # 真实命中通常 >= 85%，因此相似度门槛最低取 85
        effective_min = max(min_similarity, 85.0)
        if similarity < effective_min:
            continue

        anilist = item.get("anilist") or {}
        title_obj = anilist.get("title") or {}

        title = (
            title_obj.get("native")
            or title_obj.get("romaji")
            or title_obj.get("english")
            or str(item.get("filename", "(未知动画)"))
        )

        episode = item.get("episode", "?")
        time_from = item.get("from", 0)
        minutes = int(time_from) // 60
        seconds = int(time_from) % 60
        extra = "第" + str(episode) + "集 " + str(minutes).zfill(2) + ":" + str(seconds).zfill(2)

        anilist_id = anilist.get("id")
        url = ("https://anilist.co/anime/" + str(anilist_id)) if anilist_id else ""

        results.append(ImageSearchResult(
            title=title,
            url=url,
            similarity=similarity,
            author="",
            source_site="AniList",
            extra_info=extra,
            engine="trace.moe",
        ))

        if len(results) >= limit:
            break

    return results


# -------------------- 百度识图 --------------------

# 百度识图结果页里的站内域名与电商域名，提取来源链接时排除
_BAIDU_INTERNAL_HOSTS = (
    "baidu.com", "bdstatic.com", "bdimg.com", "bcebos.com", "baidubce.com",
)
_BAIDU_SHOPPING_HOSTS = (
    "jd.com", "3.cn", "taobao.com", "tmall.com", "yangkeduo.com", "pinduoduo.com",
    "suning.com", "vip.com", "vipshop.com", "dangdang.com", "gome.com.cn",
    "kaola.com", "mogujie.com", "1688.com", "alibaba.com", "aliexpress.com",
)


def _baidu_host_excluded(host: str) -> bool:
    """判断域名是否为百度站内链接或电商商品链接"""
    for domain in _BAIDU_INTERNAL_HOSTS + _BAIDU_SHOPPING_HOSTS:
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _collect_baidu_page_hits(page: Any) -> tuple[str, list[tuple[str, str]]]:
    """从当前百度识图结果页提取 (识别猜测, [(标题, 链接)...])。

    真实来源页是"图片来源"区的外链，按全页 <a href> 过滤"非百度站内、非电商"得到；
    百家号是内容页不算站内跳转，保留。
    """
    body_text = page.inner_text("body")
    guess_match = re.search(r'图中可能是(.+?)(?:\n|$)', body_text)
    guess = guess_match.group(1).strip() if guess_match else ""

    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in page.query_selector_all("a[href]"):
        href = (anchor.get_attribute("href") or "").strip()
        if not href.startswith(("http://", "https://")):
            continue
        host = urllib.parse.urlparse(href).netloc.lower()
        if not host or (_baidu_host_excluded(host) and not host.endswith("baijiahao.baidu.com")):
            continue
        link_key = href.split("#")[0].split("?")[0]
        if not link_key or link_key in seen:
            continue
        title = (anchor.inner_text() or "").strip()
        title = title or (anchor.get_attribute("title") or "").strip()
        if not title:
            continue
        seen.add(link_key)
        hits.append((re.sub(r"\s+", " ", title)[:120], href))
    return guess, hits


def _search_baidu(
    image_data: bytes,
    filename: str,
    limit: int,
    min_similarity: float,
    timeout: int,
) -> list[ImageSearchResult]:
    """百度识图（通过 Playwright 浏览器），擅长识别国内画师作品和角色。

    结果页结构（2026-09 实测）：识别结论"图中可能是XXX"和"图片来源"外链在
    主结果页与"相似图片"详情页都有。动漫类图片的主结果页经常两者皆无
    （相似图片区全是 graph.baidu.com 站内跳转），精确的识别结论和微博/抖音
    等真实来源页在相似图片详情页里，因此主结果页提取后要跟进前几条详情页。
    """
    import tempfile
    import time

    suffix = "." + (filename.rsplit(".", 1)[-1] if "." in filename else "jpg")
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(image_data)
        tmp.flush()
        tmp.close()
        tmp_path = tmp.name

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SearchError("百度识图需要 playwright")

        guess = ""
        source_links: list[tuple[str, str]] = []  # (标题, 链接)

        with sync_playwright() as pw:
            chromium_path = _find_chromium(pw)
            browser = pw.chromium.launch(
                executable_path=chromium_path,
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            try:
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.6099.224 Safari/537.36"
                    ),
                    locale="zh-CN",
                )
                page = ctx.new_page()
                page.set_default_timeout(timeout * 1000)

                page.goto(
                    "https://graph.baidu.com/pcpage/index?tpl_from=pc",
                    wait_until="domcontentloaded",
                    timeout=timeout * 1000,
                )
                page.wait_for_timeout(1500)

                file_inputs = page.query_selector_all('input[type="file"]')
                if not file_inputs:
                    raise SearchError("百度识图未找到上传控件")

                file_inputs[0].set_input_files(tmp_path)

                # 上传后页面会跳转到 graph.baidu.com/s? 结果页，
                # 用 URL 变化作为就绪信号，比固定 sleep 可靠
                deadline = time.monotonic() + timeout * 2
                while time.monotonic() < deadline:
                    if "graph.baidu.com/s" in page.url:
                        break
                    page.wait_for_timeout(500)
                page.wait_for_timeout(2500)  # 等结果卡片渲染完

                guess, source_links = _collect_baidu_page_hits(page)
                seen_keys = {href.split("#")[0].split("?")[0] for _, href in source_links}

                # 动漫类图片的识别结论和真实来源通常在相似图片详情页，跟进前两条
                detail_hrefs: list[str] = []
                for anchor in page.query_selector_all('a[class*="imgcol-item"]'):
                    href = (anchor.get_attribute("href") or "").strip()
                    if href.startswith("http") and "graph.baidu.com" in href:
                        detail_hrefs.append(href)
                    if len(detail_hrefs) >= 2:
                        break

                for detail_href in detail_hrefs:
                    try:
                        page.goto(
                            detail_href,
                            wait_until="domcontentloaded",
                            timeout=timeout * 1000,
                        )
                        page.wait_for_timeout(2500)
                        detail_guess, detail_hits = _collect_baidu_page_hits(page)
                        if not guess and detail_guess:
                            guess = detail_guess
                        for item in detail_hits:
                            item_key = item[1].split("#")[0].split("?")[0]
                            if item_key not in seen_keys:
                                seen_keys.add(item_key)
                                source_links.append(item)
                        if len(source_links) >= max(limit * 2, 4):
                            break
                    except Exception:
                        continue
            finally:
                browser.close()

        # ---- 解析结果 ----
        results: list[ImageSearchResult] = []

        if guess:
            # 识别结论直接挂第一个真实来源页，Agent 拿到的第一条就是可点击出处；
            # 没有来源页时才退回关键词搜索链接
            if source_links:
                guess_title, guess_url = source_links.pop(0)
                extra_info = "AI 识别结果；来源: " + guess_title
            else:
                guess_url = "https://www.baidu.com/s?wd=" + urllib.parse.quote(guess)
                extra_info = "AI 识别结果"
            results.append(ImageSearchResult(
                title="百度识别: " + guess,
                url=guess_url,
                similarity=0,
                author="",
                source_site=urllib.parse.urlparse(guess_url).netloc or "百度识图",
                extra_info=extra_info,
                engine="百度识图",
            ))

        for title, href in source_links[: max(limit * 2, 4)]:
            results.append(ImageSearchResult(
                title=title,
                url=href,
                similarity=0,
                author="",
                source_site=urllib.parse.urlparse(href).netloc,
                extra_info="图片来源",
                engine="百度识图",
            ))

        return results[:limit]

    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# -------------------- 引擎调度 --------------------

def _read_image_file(host_path: str) -> tuple[bytes, str]:
    """读取图片文件，返回 (bytes, filename)"""
    if not os.path.isfile(host_path):
        raise SearchError("图片文件不存在: " + host_path)

    file_size = os.path.getsize(host_path)
    if file_size > 20 * 1024 * 1024:
        raise SearchError("图片文件过大 (" + str(file_size // 1024 // 1024) + "MB)，最大支持 20MB")
    if file_size == 0:
        raise SearchError("图片文件为空")

    with open(host_path, "rb") as f:
        data = f.read()

    return data, os.path.basename(host_path)


def _dedup_image_results(items: list[ImageSearchResult]) -> list[ImageSearchResult]:
    """按 URL 去重（忽略锚点与查询参数），保留首次出现顺序"""
    seen_urls: set[str] = set()
    unique: list[ImageSearchResult] = []
    for r in items:
        clean_url = r.url.split("#")[0].split("?")[0] if r.url else ""
        if clean_url and clean_url in seen_urls:
            continue
        if clean_url:
            seen_urls.add(clean_url)
        unique.append(r)
    return unique


def _run_image_search(
    image_data: bytes,
    filename: str,
    limit: int,
) -> tuple[list[ImageSearchResult], list[str]]:
    """按配置的引擎顺序执行图片搜索"""
    timeout = max(3, min(int(config.TIMEOUT_SECONDS), 60))
    min_sim = config.IMAGE_SEARCH_MIN_SIMILARITY
    errors: list[str] = []

    provider = config.IMAGE_SEARCH_PROVIDER.strip().lower()

    if provider == "auto":
        providers: list[str] = []
        providers.append("saucenao")
        providers.append("iqdb")
        providers.append("tracemoe")
        providers.append("baidu")
    elif provider:
        providers = [provider]
    else:
        return [], ["未配置图片搜索引擎"]

    engine_results: list[list[ImageSearchResult]] = []

    for engine in providers:
        try:
            if engine == "saucenao":
                results = _search_saucenao(image_data, filename, limit, min_sim, timeout)
            elif engine == "iqdb":
                results = _search_iqdb(image_data, filename, limit, min_sim, timeout)
            elif engine == "tracemoe":
                results = _search_tracemoe(image_data, filename, limit, min_sim, timeout)
            elif engine == "baidu":
                results = _search_baidu(image_data, filename, limit, min_sim, timeout)
            else:
                raise SearchError("未知图片搜索引擎: " + engine)

            if results:
                # SauceNAO 高置信度命中时直接采用，不再混合其他引擎
                if engine == "saucenao" and results[0].similarity >= 85:
                    return _dedup_image_results(results)[:limit], errors
                engine_results.append(results)
        except Exception as exc:
            errors.append(engine + ": " + str(exc))
            logger.warning("星巡搜索 图片搜索 " + engine + " 失败: " + str(exc))

    if not engine_results:
        return [], errors

    # 保底收录：百度识图结果（识别结论 + 来源页）取前两条，其他引擎取最佳一条。
    # 百度识图不返回相似度，若与其他引擎结果统一按相似度排序，
    # 会把国内图片场景下最可靠的百度结果沉底甚至截掉。
    reserved: list[ImageSearchResult] = []
    rest: list[ImageSearchResult] = []
    for results in engine_results:
        if results[0].engine == "百度识图":
            reserved.extend(results[:2])
            rest.extend(results[2:])
        else:
            reserved.append(results[0])
            rest.extend(results[1:])

    # 保底名额中百度结果稳定排前，其余引擎按运行顺序跟随
    reserved.sort(key=lambda r: 0 if r.engine == "百度识图" else 1)

    # 剩余名额按相似度降序补足（百度条目相似度为 0，自然排在保底名额之后）
    rest.sort(key=lambda r: r.similarity, reverse=True)

    merged = _dedup_image_results(reserved + rest)
    return merged[:limit], errors


def _format_image_results(
    results: list[ImageSearchResult],
    errors: list[str],
) -> str:
    """格式化图片搜索结果"""
    if not results:
        details = "\n".join("- " + e for e in errors[-5:])
        return ("星巡搜索没有找到相似图片。\n" + details).strip()

    engines = "、".join(dict.fromkeys(r.engine for r in results))
    lines = ["星巡搜索（以图搜图）结果", "搜索引擎：" + engines]
    if errors:
        lines.append("部分引擎异常：" + "；".join(errors[-3:]))
    lines.append("")

    for i, r in enumerate(results, 1):
        sim_str = " (" + str(round(r.similarity, 1)) + "%)" if r.similarity > 0 else ""
        lines.append(str(i) + ". [" + r.source_site + "]" + sim_str + " " + r.title)
        if r.author and r.author != "(未知作者)":
            lines.append("   作者：" + r.author)
        if r.extra_info:
            lines.append("   信息：" + r.extra_info)
        if r.url:
            lines.append("   链接：" + r.url)

    return "\n".join(lines).strip()


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="image_search",
    description="以图搜图：搜索图片的来源、原作者和出处链接。当用户发送图片并请求搜索来源/出处/作者/原图时调用。支持 SauceNAO(Pixiv/DeviantArt/Twitter等)、IQDB(图站聚合)、trace.moe(动画截图)和百度识图(国内画师/角色识别)。",
)
async def image_search(
    _ctx: AgentCtx,
    image_path: str,
    max_results: int | None = None,
) -> str:
    """以图搜图，搜索一张图片的来源、原作者和出处链接。

    Args:
        image_path: 图片的沙箱路径，如 /app/shared/xxx.jpg。
        max_results: 返回结果数量，默认使用插件配置。

    Returns:
        包含来源站点、作者、相似度和链接的搜索结果。

    Example:
        image_search("/app/shared/received_image.jpg")
    """
    image_path = (image_path or "").strip()
    if not image_path:
        return "星巡搜索需要一个图片路径。"

    limit = max_results if max_results is not None else config.IMAGE_SEARCH_MAX_RESULTS
    limit = max(1, min(int(limit), 10))

    # 将沙箱路径转换为宿主机路径
    try:
        host_path = _ctx.fs.get_file(image_path)
    except Exception as exc:
        return "无法访问图片文件: " + str(exc)

    # 读取图片
    loop = asyncio.get_running_loop()
    try:
        image_data, fname = await loop.run_in_executor(
            None, _read_image_file, str(host_path)
        )
    except SearchError as exc:
        return str(exc)

    # 执行搜索
    results, errors = await loop.run_in_executor(
        None, _run_image_search, image_data, fname, limit
    )

    return _format_image_results(results, errors)



@plugin.mount_command(
    name="soutu",
    description="以图搜图：对最近一条图片消息进行搜图",
    aliases=["搜图", "reverse_image"],
    permission=CommandPermission.PUBLIC,
    usage="搜图 [max_results]",
    category="search",
    tags=["image", "search", "reverse"],
)
async def image_search_command(
    context: CommandExecutionContext,
    max_results: Annotated[int, Arg("返回结果数量", positional=True)] = 0,
) -> CommandResponse:
    """在最近的聊天记录中找到图片并执行以图搜图"""
    from nekro_agent.models.db_chat_message import DBChatMessage
    from nekro_agent.schemas.chat_message import ChatMessageSegmentImage, segments_from_list
    from nekro_agent.tools.path_convertor import convert_filename_to_access_path

    import json5
    from typing import cast, List, Dict

    limit = max_results if max_results > 0 else config.IMAGE_SEARCH_MAX_RESULTS
    limit = max(1, min(limit, 10))

    recent_msgs = await DBChatMessage.filter(
        chat_key=context.chat_key,
    ).order_by("-send_timestamp").limit(20)

    image_file_path = None
    for msg in recent_msgs:
        try:
            segs = segments_from_list(cast(List[Dict], json5.loads(msg.content_data)))
            for seg in segs:
                if isinstance(seg, ChatMessageSegmentImage) and seg.file_name:
                    image_file_path = str(
                        convert_filename_to_access_path(seg.file_name, context.chat_key)
                    )
                    break
        except Exception:
            continue
        if image_file_path:
            break

    if not image_file_path:
        return CmdCtl.failed("最近的消息中没有找到图片，请先发送一张图片再使用 /搜图")

    loop = asyncio.get_running_loop()
    try:
        image_data, fname = await loop.run_in_executor(
            None, _read_image_file, image_file_path
        )
    except SearchError as exc:
        return CmdCtl.failed("读取图片失败: " + str(exc))

    yield CmdCtl.message("正在搜图，请稍候...")
    results, errors = await loop.run_in_executor(
        None, _run_image_search, image_data, fname, limit
    )

    yield CmdCtl.success(_format_image_results(results, errors))


@plugin.mount_cleanup_method()
async def clean_up() -> None:
    logger.info("星巡搜索插件已清理完毕")
