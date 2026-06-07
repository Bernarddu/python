#!/usr/bin/env python3
"""Automatic market research retrieval and report generation agent.

The agent searches the web, fetches candidate pages, extracts readable snippets,
and writes a cited market report. It can run in two modes:

1. LLM mode: set OPENAI_API_KEY to generate a polished report with OpenAI.
2. Offline/template mode: without OPENAI_API_KEY, generate an extractive report
   from retrieved evidence.

Search provider priority:
- SERPAPI_API_KEY (Google via SerpAPI)
- BING_SEARCH_API_KEY (Bing Web Search)
- DuckDuckGo HTML fallback (no key, best-effort)
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable, Protocol


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; MarketReportAgent/1.0; +https://example.com/bot)"
)
DEFAULT_MODEL = "gpt-4.1-mini"


@dataclass(slots=True)
class SearchResult:
    """A single search result returned by a provider."""

    title: str
    url: str
    snippet: str = ""
    source: str = "search"


@dataclass(slots=True)
class Evidence:
    """Fetched evidence from a source page."""

    title: str
    url: str
    snippet: str
    fetched_at: str
    source: str


@dataclass(slots=True)
class ReportConfig:
    """Controls retrieval breadth and generated report shape."""

    topic: str
    geography: str = "全球"
    audience: str = "管理层"
    language: str = "zh-CN"
    horizon: str = "未来 12-24 个月"
    max_results: int = 8
    max_pages: int = 5
    timeout_seconds: int = 12
    output_path: str = "market_report.md"
    extra_questions: list[str] = field(default_factory=list)


class SearchProvider(Protocol):
    """Interface for web search providers."""

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        """Return search results for a query."""


class TextExtractor(HTMLParser):
    """Small HTML-to-text extractor based on the Python standard library."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._chunks: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "br", "li", "h1", "h2", "h3", "tr"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "canvas"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self._in_title:
            self.title = f"{self.title} {cleaned}".strip()
        self._chunks.append(cleaned)

    def text(self) -> str:
        joined = " ".join(self._chunks)
        joined = html.unescape(joined)
        joined = re.sub(r"\s+", " ", joined).strip()
        return joined


class SerpApiSearchProvider:
    """Google search through SerpAPI."""

    def __init__(self, api_key: str, timeout_seconds: int = 12) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        params = urllib.parse.urlencode(
            {
                "engine": "google",
                "q": query,
                "api_key": self.api_key,
                "num": max_results,
            }
        )
        payload = http_get_json(
            f"https://serpapi.com/search.json?{params}", self.timeout_seconds
        )
        results = []
        for item in payload.get("organic_results", [])[:max_results]:
            link = item.get("link")
            if not link:
                continue
            results.append(
                SearchResult(
                    title=item.get("title", link),
                    url=link,
                    snippet=item.get("snippet", ""),
                    source="SerpAPI",
                )
            )
        return results


class BingSearchProvider:
    """Bing Web Search provider."""

    def __init__(self, api_key: str, timeout_seconds: int = 12) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        params = urllib.parse.urlencode(
            {"q": query, "count": max_results, "mkt": "zh-CN"}
        )
        request = urllib.request.Request(
            f"https://api.bing.microsoft.com/v7.0/search?{params}",
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Ocp-Apim-Subscription-Key": self.api_key,
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        results = []
        for item in payload.get("webPages", {}).get("value", [])[:max_results]:
            link = item.get("url")
            if not link:
                continue
            results.append(
                SearchResult(
                    title=item.get("name", link),
                    url=link,
                    snippet=item.get("snippet", ""),
                    source="Bing",
                )
            )
        return results


class DuckDuckGoHtmlSearchProvider:
    """DuckDuckGo HTML fallback provider that does not require an API key."""

    def __init__(self, timeout_seconds: int = 12) -> None:
        self.timeout_seconds = timeout_seconds

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        params = urllib.parse.urlencode({"q": query})
        body = http_get_text(
            f"https://duckduckgo.com/html/?{params}", self.timeout_seconds
        )
        blocks = re.findall(
            r'<a rel="nofollow" class="result__a" href="(?P<href>[^"]+)">(?P<title>.*?)</a>',
            body,
            flags=re.S,
        )
        snippets = re.findall(
            r'<a class="result__snippet".*?>(?P<snippet>.*?)</a>', body, flags=re.S
        )
        results = []
        for index, (href, title) in enumerate(blocks[:max_results]):
            url = html.unescape(href)
            parsed = urllib.parse.urlparse(url)
            query_params = urllib.parse.parse_qs(parsed.query)
            if "uddg" in query_params:
                url = query_params["uddg"][0]
            snippet = snippets[index] if index < len(snippets) else ""
            results.append(
                SearchResult(
                    title=clean_html_fragment(title),
                    url=url,
                    snippet=clean_html_fragment(snippet),
                    source="DuckDuckGo",
                )
            )
        return results


def clean_html_fragment(fragment: str) -> str:
    """Remove tags and normalize a short HTML fragment."""

    without_tags = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def http_get_text(url: str, timeout_seconds: int) -> str:
    """Fetch UTF-8-ish text from a URL."""

    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read(2_000_000)
    return raw.decode("utf-8", errors="replace")


def http_get_json(url: str, timeout_seconds: int) -> dict:
    """Fetch JSON from a URL."""

    return json.loads(http_get_text(url, timeout_seconds))


def choose_search_provider(timeout_seconds: int) -> SearchProvider:
    """Select the best configured search provider from environment variables."""

    if os.getenv("SERPAPI_API_KEY"):
        return SerpApiSearchProvider(os.environ["SERPAPI_API_KEY"], timeout_seconds)
    if os.getenv("BING_SEARCH_API_KEY"):
        return BingSearchProvider(os.environ["BING_SEARCH_API_KEY"], timeout_seconds)
    return DuckDuckGoHtmlSearchProvider(timeout_seconds)


def build_queries(config: ReportConfig) -> list[str]:
    """Create diversified retrieval queries for market research."""

    base = (
        f"{config.topic} market report {config.geography} "
        "market size growth trends competitors 2025 2026"
    )
    queries = [
        base,
        f"{config.topic} {config.geography} market size CAGR forecast",
        f"{config.topic} industry trends regulation investment {config.geography}",
        f"{config.topic} key companies market share {config.geography}",
    ]
    queries.extend(config.extra_questions)
    return queries


def deduplicate_results(results: Iterable[SearchResult]) -> list[SearchResult]:
    """Deduplicate search results by normalized URL."""

    seen: set[str] = set()
    unique: list[SearchResult] = []
    for result in results:
        normalized = normalize_url(result.url)
        if normalized in seen or not normalized.startswith(("http://", "https://")):
            continue
        seen.add(normalized)
        unique.append(result)
    return unique


def normalize_url(url: str) -> str:
    """Normalize URLs for deduplication."""

    parsed = urllib.parse.urlparse(url)
    clean_query = urllib.parse.urlencode(
        [
            (key, value)
            for key, value in urllib.parse.parse_qsl(parsed.query)
            if not key.lower().startswith(("utm_", "fbclid", "gclid"))
        ]
    )
    return urllib.parse.urlunparse(
        (
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",
            clean_query,
            "",
        )
    )


def fetch_evidence(result: SearchResult, timeout_seconds: int) -> Evidence | None:
    """Fetch a result URL and extract a compact evidence snippet."""

    try:
        page = http_get_text(result.url, timeout_seconds)
    except (urllib.error.URLError, TimeoutError, OSError):
        if result.snippet:
            return Evidence(
                title=result.title,
                url=result.url,
                snippet=result.snippet,
                fetched_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                source=result.source,
            )
        return None

    extractor = TextExtractor()
    extractor.feed(page)
    text = extractor.text()
    snippet = select_relevant_excerpt(text, result.snippet)
    title = extractor.title or result.title
    if not snippet:
        return None
    return Evidence(
        title=title[:180],
        url=result.url,
        snippet=snippet,
        fetched_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        source=result.source,
    )


def select_relevant_excerpt(text: str, fallback: str, max_chars: int = 1200) -> str:
    """Pick a compact excerpt with market-research keywords."""

    if not text:
        return fallback[:max_chars]
    sentences = re.split(r"(?<=[。！？.!?])\s+", text)
    keywords = {
        "market",
        "growth",
        "forecast",
        "cagr",
        "size",
        "share",
        "revenue",
        "trend",
        "市场",
        "增长",
        "规模",
        "预测",
        "份额",
        "收入",
        "趋势",
        "竞争",
    }
    scored: list[tuple[int, str]] = []
    for sentence in sentences:
        lower = sentence.lower()
        score = sum(1 for keyword in keywords if keyword in lower)
        if 80 <= len(sentence) <= 500:
            scored.append((score, sentence))
    picked = [
        sentence
        for score, sentence in sorted(scored, reverse=True)[:4]
        if score > 0
    ]
    excerpt = " ".join(picked) or text[:max_chars]
    return excerpt[:max_chars].strip()


def retrieve_evidence(config: ReportConfig) -> list[Evidence]:
    """Search, fetch, and extract evidence for the configured report."""

    provider = choose_search_provider(config.timeout_seconds)
    all_results: list[SearchResult] = []
    per_query = max(2, config.max_results // 2)
    for query in build_queries(config):
        try:
            all_results.extend(provider.search(query, per_query))
            time.sleep(0.3)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            continue
    unique_results = deduplicate_results(all_results)[: config.max_results]

    evidence: list[Evidence] = []
    for result in unique_results:
        item = fetch_evidence(result, config.timeout_seconds)
        if item:
            evidence.append(item)
        if len(evidence) >= config.max_pages:
            break
    return evidence


def format_evidence(evidence: list[Evidence]) -> str:
    """Format evidence for a prompt or template report."""

    lines = []
    for index, item in enumerate(evidence, start=1):
        lines.append(
            textwrap.dedent(
                f"""
                [{index}] {item.title}
                URL: {item.url}
                Retrieved: {item.fetched_at}
                Excerpt: {item.snippet}
                """
            ).strip()
        )
    return "\n\n".join(lines)


def generate_report(config: ReportConfig, evidence: list[Evidence]) -> str:
    """Generate the final market report."""

    if os.getenv("OPENAI_API_KEY"):
        return generate_report_with_openai(config, evidence)
    return generate_template_report(config, evidence)


def generate_report_with_openai(config: ReportConfig, evidence: list[Evidence]) -> str:
    """Generate a cited report using the OpenAI Responses API."""

    model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    prompt = build_llm_prompt(config, evidence)
    payload = {
        "model": model,
        "input": prompt,
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=max(30, config.timeout_seconds * 3)) as response:
        data = json.loads(response.read().decode("utf-8"))
    return extract_openai_text(data)


def build_llm_prompt(config: ReportConfig, evidence: list[Evidence]) -> str:
    """Build a grounded prompt for market-report generation."""

    return textwrap.dedent(
        f"""
        你是一名资深市场研究分析师。请基于下方检索证据，为{config.audience}生成一份结构化市场报告。

        要求：
        - 使用语言：{config.language}
        - 主题：{config.topic}
        - 地域：{config.geography}
        - 时间范围：{config.horizon}
        - 只使用证据中支持的信息；如果证据不足，请明确写“不足以判断”。
        - 每个关键事实后用 [1]、[2] 这样的编号引用证据。
        - 输出 Markdown，包含：执行摘要、市场规模与增长、需求/供给驱动、竞争格局、客户与渠道、政策/技术/宏观风险、机会清单、行动建议、附录：来源。

        检索证据：
        {format_evidence(evidence)}
        """
    ).strip()


def extract_openai_text(data: dict) -> str:
    """Extract text from a Responses API payload."""

    if data.get("output_text"):
        return data["output_text"]
    chunks: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(content["text"])
    return "\n".join(chunks).strip()


def generate_template_report(config: ReportConfig, evidence: list[Evidence]) -> str:
    """Generate a deterministic extractive report when no LLM key is configured."""

    today = dt.date.today().isoformat()
    source_lines = [
        f"{idx}. [{item.title}]({item.url})（{item.source}，{item.fetched_at}）"
        for idx, item in enumerate(evidence, 1)
    ]
    evidence_lines = [
        f"- [{idx}] {item.snippet}" for idx, item in enumerate(evidence, 1)
    ]
    if not evidence:
        evidence_lines = [
            "- 未检索到可用证据；请配置 SERPAPI_API_KEY 或 "
            "BING_SEARCH_API_KEY 后重试。"
        ]
        source_lines = ["无"]

    return textwrap.dedent(
        f"""
        # {config.topic}市场报告

        - 生成日期：{today}
        - 地域：{config.geography}
        - 目标读者：{config.audience}
        - 研判周期：{config.horizon}
        - 生成模式：模板模式（未检测到 OPENAI_API_KEY）

        ## 1. 执行摘要

        本报告基于自动检索得到的 {len(evidence)} 条公开来源证据生成。由于当前未启用大模型生成，以下内容以证据摘录、分析框架和行动建议为主；建议配置 `OPENAI_API_KEY` 以获得更完整的综合分析。

        ## 2. 关键证据摘录

        {chr(10).join(evidence_lines)}

        ## 3. 市场规模与增长

        - 请优先核验来源中关于市场规模、收入、出货量、CAGR 与预测年份的数据。
        - 如果多个来源口径不一致，应按地域、产品定义、统计口径和预测区间拆分比较。

        ## 4. 需求与供给驱动

        - 需求侧：关注客户预算、渗透率、替代方案、价格敏感度和政策补贴。
        - 供给侧：关注核心供应商、产能、渠道覆盖、技术路线和单位经济性。

        ## 5. 竞争格局

        - 梳理头部企业、区域玩家、新进入者与上下游合作伙伴。
        - 建议补充访谈或数据库验证市场份额，避免仅凭新闻稿判断竞争强弱。

        ## 6. 机会与风险

        - 机会：高增长细分场景、监管明确化、技术降本、渠道整合。
        - 风险：需求放缓、价格战、政策变化、供应链约束、数据口径不透明。

        ## 7. 建议下一步

        1. 对排名前三的来源做人工复核，确认数据口径。
        2. 补充 3-5 位行业专家或客户访谈。
        3. 建立竞品价格、产品能力和渠道矩阵。
        4. 用最新财报、海关/协会/政府数据验证市场规模。

        ## 8. 来源

        {chr(10).join(source_lines)}
        """
    ).strip() + "\n"


def write_report(path: str, content: str) -> None:
    """Write the report to disk, creating parent directories when needed."""

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def parse_args() -> ReportConfig:
    """Parse CLI arguments into a report config."""

    parser = argparse.ArgumentParser(
        description="自动检索公开信息并生成带来源引用的市场报告。"
    )
    parser.add_argument("topic", help="市场主题，例如：'中国新能源汽车充电桩'")
    parser.add_argument("--geography", default="全球", help="地域范围，默认：全球")
    parser.add_argument("--audience", default="管理层", help="目标读者，默认：管理层")
    parser.add_argument("--language", default="zh-CN", help="输出语言，默认：zh-CN")
    parser.add_argument("--horizon", default="未来 12-24 个月", help="研判周期")
    parser.add_argument("--max-results", type=int, default=8, help="最多保留的搜索结果数")
    parser.add_argument("--max-pages", type=int, default=5, help="最多抓取的页面数")
    parser.add_argument("--timeout-seconds", type=int, default=12, help="单次请求超时时间")
    parser.add_argument("--output", default="market_report.md", help="报告输出路径")
    parser.add_argument(
        "--question",
        action="append",
        default=[],
        help="额外检索问题，可重复传入。",
    )
    args = parser.parse_args()
    return ReportConfig(
        topic=args.topic,
        geography=args.geography,
        audience=args.audience,
        language=args.language,
        horizon=args.horizon,
        max_results=args.max_results,
        max_pages=args.max_pages,
        timeout_seconds=args.timeout_seconds,
        output_path=args.output,
        extra_questions=args.question,
    )


def main() -> None:
    """Run the market report agent from the command line."""

    config = parse_args()
    evidence = retrieve_evidence(config)
    report = generate_report(config, evidence)
    write_report(config.output_path, report)
    print(f"已生成报告：{config.output_path}（来源数：{len(evidence)}）")


if __name__ == "__main__":
    main()
