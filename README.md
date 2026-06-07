# 自动检索生成市场报告的 Agent

这是一个纯 Python、可命令行运行的市场研究 Agent。它会自动生成检索式、搜索公开网页、抓取候选页面、抽取证据，并输出带来源的 Markdown 市场报告。

## 功能

- 自动围绕市场规模、增长、趋势、竞争格局和政策/投资信号生成检索式。
- 支持 SerpAPI、Bing Web Search；未配置搜索 API 时使用 DuckDuckGo HTML 作为兜底。
- 支持 OpenAI Responses API 生成结构化中文市场报告。
- 未配置 `OPENAI_API_KEY` 时，也会输出可复核的模板报告和证据摘录。
- 仅依赖 Python 标准库，便于快速部署和二次开发。

## 环境变量

推荐至少配置一个搜索 API Key 和一个 OpenAI API Key：

```bash
export OPENAI_API_KEY="你的 OpenAI API Key"
export OPENAI_MODEL="gpt-4.1-mini"        # 可选
export SERPAPI_API_KEY="你的 SerpAPI Key" # 可选，优先级最高
export BING_SEARCH_API_KEY="你的 Bing Key" # 可选，未设置 SerpAPI 时使用
```

如果没有配置搜索 API Key，Agent 会使用 DuckDuckGo HTML 兜底；如果没有配置 `OPENAI_API_KEY`，Agent 会使用模板模式生成报告。

## 使用方法

```bash
python market_report_agent.py "中国新能源汽车充电桩" \
  --geography "中国" \
  --audience "投资委员会" \
  --horizon "未来 24 个月" \
  --max-results 10 \
  --max-pages 6 \
  --output reports/charging_market_report.md
```

你也可以追加更具体的问题来引导检索：

```bash
python market_report_agent.py "AI PC" \
  --geography "全球" \
  --question "AI PC shipment forecast 2026" \
  --question "AI PC key vendors market share" \
  --output ai_pc_report.md
```

## 输出结构

启用 OpenAI 后，报告会包含：

1. 执行摘要
2. 市场规模与增长
3. 需求/供给驱动
4. 竞争格局
5. 客户与渠道
6. 政策/技术/宏观风险
7. 机会清单
8. 行动建议
9. 附录：来源

## 注意事项

- 自动检索报告适合作为初稿，不应替代人工尽调。
- 对市场规模、CAGR、份额等数字，建议用政府、协会、财报或付费数据库做二次验证。
- 抓取公开网页时请遵守目标网站的服务条款与 robots 规则。
