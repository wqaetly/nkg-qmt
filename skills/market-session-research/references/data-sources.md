# 数据来源与本机接入

以下为 2026-11-10 的实测和代码观察，接口能力可能变化。每次使用不熟悉的接口先读当前官方文档，按所需字段调用；不要把实测覆盖推成全量覆盖承诺。

## 凭据与读取方式

Tushare 使用项目 `.env` 的 `TUSHARE_TOKEN`。可在项目目录从 Python 导入 `qmt.config.load_env` 后调用 `qmt.market.Tushare`；不要打印 `.env`、环境变量或请求认证头。

Fuyao 优先使用环境变量 `FUYAO_API_KEY`（也可在被 Git 忽略的项目 `.env` 中配置并通过 `load_env()` 加载）。若用户明确指定了本地凭据文件，只读取指定文件，不搜索其他应用缓存；路径不写入仓库。对于用户说明的“服务网址下一条非空行为 key”的格式，在进程内以 UTF-8-SIG 读取，找出去除首尾空白和结尾斜杠后**完全等于** `https://fuyao.aicubes.cn` 的行，取下一条非空行；匹配不存在、不唯一或无下一行时停止该来源并说明需要配置。禁止整体输出凭据文件，也不要猜选其他密钥。使用后不把 key 写入日志、提示词、报告、命令行参数、skill 或记忆。

Fuyao 认证为 HTTPS GET 的 `X-api-key` 请求头，固定目标主机 `fuyao.aicubes.cn`，拒绝重定向。可复用 `qmt.market.request_json`（有 HTTPS 校验、拒绝重定向、超时、8MB 响应限制和脱敏错误），用 `urllib.parse.urlencode` 构建非敏感查询参数。返回体必须是对象、`code == 0` 且 `data` 符合当前接口文档；空数据、截断、错误不等于成功。不要把上游原始错误正文打印出来。

每个来源先做一次必要的小请求验证权限；遵守官方频率限制，复用本次已取得的数据，不为检查可用性重复大量请求。单个来源失败时保留来源标签与缺口，不能偷偷冒充另一个来源。不要自动安装 SDK、下载全市场包或改动现有 token 配置。

## 分工与接口

官方入口：[Fuyao 文档索引](https://fuyao.aicubes.cn/llms.txt)、[完整文档](https://fuyao.aicubes.cn/llms-full.txt)、[最佳实践](https://fuyao.aicubes.cn/best-practices/)、[Tushare 文档](https://tushare.pro/document/2)。最佳实践的演示收益可能来自模拟数据，不能作为策略有效性证据。

| 需求 | 首选及补充 | 重要限制 |
| --- | --- | --- |
| 盘中股票行情 | Fuyao `/api/a-share/prices/snapshot`，参数 `thscodes` | 核验每个标的代码、行情时间、交易状态；请求成功时间不等于行情时间 |
| 股票日线 | Fuyao `/api/a-share/prices/historical`，`thscode,interval=1d,start,end,adjust`；或 Tushare `daily` | Fuyao start/end 为毫秒时间戳；执行参考价用 `adjust=none`；趋势比较注明复权方式 |
| 最新估值 | Fuyao `/api/a-share/valuations/snapshot`，`thscodes`；Tushare `daily_basic` | Fuyao 实测 PE/PB/PS/PCF 快照，不能直接代替历史估值分位、股息率、总股本 |
| 财务 | Fuyao `/api/a-share/financials/income-statements`、`balance-sheets`、`cash-flow-statements`；参数以当前文档为准 | 合并字段较少；报告期与披露/修订时间不同，不能声称天然满足历史时点回测 |
| 财务细项、审计、分红 | Tushare `income,balancesheet,cashflow,fina_indicator,fina_audit,dividend`；公司原始公告 | 对关键值以公告交叉核验；积分权限不足明确缺失 |
| 日历 | Fuyao calendar 文档接口或 Tushare `trade_cal` | 按市场分别核验；不要把 A 股交易日套用到港美股 |
| ETF | Fuyao fund-market 文档接口、Tushare 基金接口；基金公司及指数公司文件 | 实测 Fuyao ETF 历史行情近 5 年前复权，返回 adjust 为 null 也不能认定未复权；盘中市价、净值、IOPV 分开 |
| 公告、新闻、基金规则 | 公司/交易所/基金公司/监管与统计机关网页；Gemini 找线索 | 搜索摘要或媒体转述不能单独支撑决定行动的结论 |

财务路径使用前从 [financials 文档](https://fuyao.aicubes.cn/docs/api-reference/financials/) 确认。相关专题：[prices](https://fuyao.aicubes.cn/docs/api-reference/prices/)、[valuations](https://fuyao.aicubes.cn/docs/api-reference/valuations/)、[calendar](https://fuyao.aicubes.cn/docs/api-reference/calendar/)、[corporate-actions](https://fuyao.aicubes.cn/docs/api-reference/corporate-actions/)、[fund-market](https://fuyao.aicubes.cn/docs/api-reference/fund-market/)。

本项目可复用的只读入口：
- `Tushare.call(api, params, fields)`：返回字典行，校验返回字段、错误与满页风险；优先 `Tushare()` 不使用跨日无期限缓存。
- `Tushare.basics()`：当前上市股票字典，不能用于历史存活者无偏回测。
- `Tushare.calendar(start, end)`：日期为 `YYYYMMDD`，返回开市日期。盘中查询最近完整日需选择严格早于今天的最近开市日。
- `Tushare.prices(as_of)`：`daily_basic`，是指定交易日的日频估值，**不是盘中报价**。
- `Tushare.dossier(symbol, as_of, start_year=None)`：股票财务/分红/审计底稿；先读当前实现和参数。`as_of` 为信息截止日，不要为了配合日线日期而遗漏今天已发布的公告。
- `qmt.config.now_cn()`、`atomic_json(path, value)` 可用于时间和结果落盘。`qmt/` 仅提供只读数据助手，研究编排由本 skill 完成。

`dossier` 会把各子接口失败放入 `errors`，返回对象不代表所有子项成功。其分红请求未按截止日过滤，其他财报日期检查也不保证盘中可知性：消费数据时另查公告/实施/修订时间，剔除晚于截止时间的内容；只有日期而无发布时间时，不能断言当天盘中已经可知。默认财务历史较长，可按研究需要显式缩短 `start_year`。最近完整交易日仍可能尚未发布数据；应标出实际取得的最近日期和缺口，不能把空日频表当作全市场没有候选。

## 不可混淆的口径

- Fuyao 股票成交量为股、成交额为元；Tushare `daily` 的 vol 为手、amount 为千元。跨源转换时保留原值及原单位，按当前字段文档确认。
- Fuyao `total_debt` 实测对应负债合计，不能当作有息债务计算净现金。银行保险不机械套用工业企业自由现金流指标。
- 分红除权数据不是完整公告事件表。实测中国神华 2017 年分红合并了普通与特别股息，不能把一次性特别股息外推为长期股息率。提案、已批准、已实施、除权、到账分别核验。
- 估值历史分位需要真实一致口径历史序列；盈利负数的 PE 不能直接排序成便宜。过去财报修订值不能自动用于“当时可知”分析。
- Tushare 与 Fuyao 不可仅替换 base URL：认证、参数、单位、字段与能力均不同。Fuyao 目前只验证了数据能力，不是下单通道。
- QDII 净值可能滞后且跨时区，不能拿陈旧净值计算的比率称为准确实时溢价。说明市价/净值时点、汇率、标的交易时段与估算限制。

## 研究包的最小证据记录

每条关键数据保留：`symbol, market, currency, metric, value, unit, period, announced_at, data_as_of, fetched_at, source, source_url_or_endpoint`；未知填 null 并说明。API 参数可记录但去除凭据和临时签名 URL。计算指标另列公式、输入证据和假设。行情缺少时间或已明显过期时可用于标注日期的背景研究，不能用于“现在即可按此价格操作”的结论。
