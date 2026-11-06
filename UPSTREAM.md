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

## 自动模拟扩展

自动模拟的盈利代理、历史支付率、财务阈值、月度仓位和买卖规则由本项目新增，不属于上游已验证策略；没有把机器输出设为人工研究确认。

- [Tushare 基金复权因子](https://tushare.pro/document/2?doc_id=199)：使用 fund_daily 收盘价乘 fund_adj 因子构造同期参考变化。
- [易方达中证红利 ETF 产品资料概要（2026-03-23）](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-23/515180_20260323_XCTC.pdf)：515180 跟踪中证红利指数，股票型风险，投资者交易费用按券商实际收取；只是比较基准，不代表推荐购买。
- [OpenAI 定时任务文档](https://learn.chatgpt.com/docs/automations?surface=app)：本机定时工作依赖电脑开机及桌面应用运行。

## v2 选股与数据修正

- 上游方法不要求候选来自中证红利。本项目 v2 使用沪深主板分层筛选；3% 历史股息率、同行 PE 中位数、三年 PE 分位 50%、盈利代理 PE 25 倍、20 日成交额中位数 5,000 万元，均为本项目实验参数，并非上游结论。
- [Tushare 日线字段](https://tushare.pro/document/2?doc_id=27)：成交额 amount 为千元，成交量 vol 为手；停牌期间不提供数据。
- [Tushare 每日指标](https://tushare.pro/document/2?doc_id=32)：PE/PB 与历史股息率字段；只对正 PE 样本做同行/历史比较。
- [青岛啤酒 2023-022 年度 A 股权益分派实施公告（公司公告镜像）](https://file.finance.sina.com.cn/hkdata98/hk/hk_realtime_announcement/news/20230706/10809161-0.PDF)，2023-07-06，PDF 第 2—3 页：2022 年普通每股 1.30 元、特别 0.50 元，共计 1.80 元；登记日 2023-07-13、除息及派息日 2023-07-14。仅用于修正已知重复组成项，不作为自动批准或未来分红承诺。

## 研究池与历史回放

- 研究方法沿用上游 `dividend-stock-research`，但20只研究池、排序、固定仓位、4%/5%/6%回放对照和10%年化研究目标为本项目新增实验，并非上游收益承诺。
- [Tushare 股票列表](https://tushare.pro/document/2?doc_id=25)：分别查询上市、退市、暂停上市证券，使用上市/退市日期重建历史集合，不以今天仍上市的列表代替。
- [Tushare 股票曾用名](https://tushare.pro/document/2?doc_id=100)：按名称生效区间和公告日期核对当时名称；不能用今天的ST名称剔除过去尚未发生的风险。
- 历史成交使用原始OHLC加现金分红，不将复权收益与现金派息重复相加；ETF复权比较属于另一口径。原始数据缓存、协议、候选淘汰理由、逐月信号与结果均留在本地独立实验目录。
