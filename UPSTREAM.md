# 来源与数据口径

## 研究技能

`skills/dividend-stock-research/` 从 [wqaetly/dividend-stock-research](https://github.com/wqaetly/dividend-stock-research/tree/1cd5475314fefdcedb9fcf7bcc4c3fe4fd1f8e41) 导入，版本 `1cd5475314fefdcedb9fcf7bcc4c3fe4fd1f8e41`，核验日期 2026-11-10。保留原始 [MIT 许可证](skills/dividend-stock-research/LICENSE)、计算器、测试和更早上游的 [来源说明](skills/dividend-stock-research/UPSTREAM.md)。

本库增加 Tushare HTTP/Fuyao 接入说明及与 `market-session-research` 的协作入口。股息计算器的数学逻辑未改动，仅用于情景算术；分析方法不代表经过验证的收益策略。

`skills/market-session-research/` 用于编排数据筛选、Gemini 网页 Deep Research 和 Codex 独立核验。它不包含模型 API 调用、交易执行或固定选股算法。默认报告时间是使用偏好，不代表已经配置了定时任务。

## 只读数据

- [Tushare HTTP 协议](https://tushare.pro/document/1?doc_id=130)：本库 `qmt.market` 通过 HTTPS 查询，拒绝重定向并隐藏上游错误原文，避免暴露凭据。
- [分红字段](https://tushare.pro/document/2?doc_id=103)：`cash_div_tax` 为税前元/股，`stk_div` 为每股送转；提案、实施、登记、除息和到账日期分别核验。
- [利润表](https://tushare.pro/document/2?doc_id=33)、[资产负债表](https://tushare.pro/document/2?doc_id=36)、[现金流](https://tushare.pro/document/2?doc_id=44)、[财务指标](https://tushare.pro/document/2?doc_id=79)：区分报告期、公告/修订日期、合并口径与累计期间。缺失不视为零。
- [Fuyao 官方文档](https://fuyao.aicubes.cn/llms.txt)：接口与单位差异、已知覆盖限制详见 [数据接入说明](skills/market-session-research/references/data-sources.md)。
- [Gemini Deep Research 官方说明](https://support.google.com/gemini/answer/15719111?hl=en)：实际功能、访问条件和额度以当前页面为准。

行情的请求时间不等于行情时间，财报所属期不等于市场可知日期；当前抓到的修订数据不能自动视为当时公开版本。原始数据、参数、单位与取数时间保存在本地研究包中，去除密钥和签名链接。第三方数据库的再分发权不随代码许可转移。
