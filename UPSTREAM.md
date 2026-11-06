# 来源与口径

研究方法及情景计算来自 https://github.com/wqaetly/dividend-stock-research ，参考提交
`1cd5475314fefdcedb9fcf7bcc4c3fe4fd1f8e41`，访问日期 2026-11-06。
MIT 许可保存在 `vendor/dividend_stock_research/LICENSE`。
计算器未改动；文档和上游来源说明一并保留。测试另外验证其虚构算例和参数边界。

该来源提供研究方法而非已验证的交易策略。本项目添加结构化初筛、研究确认失效检查、
用户指定的交易计划、模拟和手动账本。没有将研究方法包装成经过收益回测的策略。

## 数据定义

- [Tushare HTTP](https://tushare.pro/document/1?doc_id=130)：直接调用标准 HTTP 接口，当前宿主未提供 Tushare MCP。
- [指数权重](https://tushare.pro/document/2?doc_id=96)：000922.CSI，最新月度快照；不能声称逐日实时成分。
- [中证红利](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000922factsheet.pdf)：100 个样本。
- [分红](https://tushare.pro/document/2?doc_id=103)：`cash_div_tax` 为税前元/股，`stk_div` 为每股送转。
- [利润表](https://tushare.pro/document/2?doc_id=33)、[资产负债](https://tushare.pro/document/2?doc_id=36)、[现金流](https://tushare.pro/document/2?doc_id=44)、[指标](https://tushare.pro/document/2?doc_id=79)。财报金额为元，资产负债表 total_share 为股；daily_basic total_share 为万股。
- 财报只取合并累计类型 1/4，按报告期选择截至日已披露的较新版本，同日优先调整合并报表；不同版本的实质冲突停止确定结论。累计季报不相加。
- [印花税减半公告](https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html)：默认模拟卖出税率 0.0005；佣金等其余默认值仅是教学假设，须按实际券商费率校准。

原始接口结果、参数、字段、取数时间仅保存在被忽略的 data/cache 下，不带 token。
第三方数据许可不随代码的开源许可转移，不将行情和私人报告推送到公开仓库。
