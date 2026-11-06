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
- [分红](https://tushare.pro/document/2?doc_id=103)：`cash_div_tax` 为税前元/股，`stk_div` 为每股送转；`ann_date` 为预案/决案公告日期，`imp_ann_date` 为实施公告日期，`record_date`、`ex_date`、`pay_date` 分别对应登记、除息和付款。公告已知不等于现金已经收到。
- [利润表](https://tushare.pro/document/2?doc_id=33)、[资产负债](https://tushare.pro/document/2?doc_id=36)、[现金流](https://tushare.pro/document/2?doc_id=44)、[指标](https://tushare.pro/document/2?doc_id=79)。财报金额为元，资产负债表 total_share 为股；daily_basic total_share 为万股。
- 财报当前只取合并累计类型 1/4，按报告期选择截至日已披露的较新版本，同日优先调整合并报表；不同版本的实质冲突停止确定结论。累计季报不相加。官方利润表另定义类型5为调整前合并原数据；现有选择逻辑和公告日期过滤尚不能证明已完整还原当时所有原始版本。
- [印花税减半公告](https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html)：默认模拟卖出税率 0.0005；佣金等其余默认值仅是教学假设，须按实际券商费率校准。

当前默认工作目录为 `data/capital-100k/`，原始接口记录、参数、字段、取数时间保存在其中的 `cache/` 或 `backtests/source-cache/` 下，不带 token。旧 `data/cache/`、公开研究资料及 `data/backtests/source-cache/` 仅复制复用到新工作目录，旧账本没有复制；原 `data/` 结果继续保留。
第三方数据许可不随代码的开源许可转移，不将行情和私人报告推送到公开仓库。

## 自动模拟扩展

自动模拟的盈利代理、历史支付率、财务阈值、月度仓位和买卖规则由本项目新增，不属于上游已验证策略；没有把机器输出设为人工研究确认。当前 `value-dividend-v3` 使用10万元虚拟本金、最多20只持仓、10%现金预留、单股5,000元及单行业20,000元上限，初始目标每股约4,500元。新账本位于独立的 `data/capital-100k/auto/value-dividend-v3/`，旧版1万元账本保留，不合并收益。

- [Tushare 基金复权因子](https://tushare.pro/document/2?doc_id=199)：使用 fund_daily 收盘价乘 fund_adj 因子构造同期参考变化。
- [易方达中证红利 ETF 产品资料概要（2026-03-23）](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-23/515180_20260323_XCTC.pdf)：515180 跟踪中证红利指数，股票型风险，投资者交易费用按券商实际收取；只是比较基准，不代表推荐购买。
- [OpenAI 定时任务文档](https://learn.chatgpt.com/docs/automations?surface=app)：本机定时工作依赖电脑开机及桌面应用运行。

持仓数量资料于2026-11-06核对：

- [Qlib官方组合策略示例](https://qlib.readthedocs.io/en/latest/component/strategy.html)：`TopkDropoutStrategy` 示例使用 `topk=50`，这是示例参数，不是所有量化策略的标准持仓数，也不是本项目策略的收益证据。
- [Investor.gov：资产配置与分散化](https://www.investor.gov/introduction-investing/getting-started/asset-allocation)：分散化还应覆盖不同公司、行业及资产类别；即使持有多个基金也需检查重叠和行业集中。本项目选择最多20只，是结合10万元、整手交易及20只研究池的实验设置，不宣称已经充分分散或属于统一行业标准。合格候选不足时不凑单，每页5只仅是展示设置。

## v2 选股与数据修正

- 上游方法不要求候选来自中证红利。本项目 v2 使用沪深主板分层筛选；3% 历史股息率、同行 PE 中位数、三年 PE 分位 50%、盈利代理 PE 25 倍、20 日成交额中位数 5,000 万元，均为本项目实验参数，并非上游结论。
- [Tushare 日线字段](https://tushare.pro/document/2?doc_id=27)：成交额 amount 为千元，成交量 vol 为手；停牌期间不提供数据。
- [Tushare 每日指标](https://tushare.pro/document/2?doc_id=32)：PE/PB 与历史股息率字段；只对正 PE 样本做同行/历史比较。`dv_ttm` 官方口径同时限定除息日在近12个月、分红报告期在12个月以内，不能直接当作未来一年预计股息，也不应与按付款日自行滚动求和的指标混名。
- [青岛啤酒 2023-022 年度 A 股权益分派实施公告（公司公告镜像）](https://file.finance.sina.com.cn/hkdata98/hk/hk_realtime_announcement/news/20230706/10809161-0.PDF)，2023-07-06，PDF 第 2—3 页：2022 年普通每股 1.30 元、特别 0.50 元，共计 1.80 元；登记日 2023-07-13、除息及派息日 2023-07-14。仅用于修正已知重复组成项，不作为自动批准或未来分红承诺。

## 研究池与历史回放

- 研究方法沿用上游 `dividend-stock-research`，但20只研究池、排序、固定仓位、4%/5%/6%回放对照和10%年化研究目标为本项目新增实验，并非上游收益承诺。
- [Tushare 股票列表](https://tushare.pro/document/2?doc_id=25)：分别查询上市、退市、暂停上市证券，使用上市/退市日期重建历史集合，不以今天仍上市的列表代替。
- [Tushare 股票曾用名](https://tushare.pro/document/2?doc_id=100)：按名称生效区间和公告日期核对当时名称；不能用今天的ST名称剔除过去尚未发生的风险。
- 历史成交使用原始OHLC加现金分红，不将复权收益与现金派息重复相加；ETF复权比较属于另一口径。原始数据缓存、协议、候选淘汰理由、逐月信号与结果均留在本地独立实验目录。

## 历史行业与纯送转核验（v3）

- [Tushare申万行业成分](https://tushare.pro/document/2?doc_id=335)：分别取得最新/历史成员，程序按 `in_date <= 日期 < out_date` 匹配一级行业；空缺或不同一级行业同时生效均不使用当前标签补齐。单批2000条上限触发停止，不接受截断同行集合。官方文档只给出纳入/剔除及是否最新字段，并未承诺完整历史分类版本或逐日原始发布档案；该区间匹配是程序处理规则，不是PIT正确性的保证。已发现600882历史业务冲突，见下节。
- [中远海控2021-028实施公告（公司公告镜像）](https://pdf.dfcfw.com/pdf/H2_AN202107071502359554_1.pdf)，2021-07-08，第1—2页：2020年度资本公积每股转增0.3股，无现金派息，新增股2021-07-15上市。
- [方大特钢2020年年报](https://static.cninfo.com.cn/finalpage/2021-03-17/1209393844.PDF)，2021-03-17，第24、40页：2019年度现金股利为0、每10股转增4.9股，新增股份2020-04-10上市。修正仅自年报披露日起生效，不回填到较早的决策时点。
- 两笔修正保留原始字段、SHA256、证据生效日期及来源页码，不填造付款日。股本冲突没有被忽略或选取有利值，仍需原文复核。

## 往年数据的方法依据与已核实冲突

以下资料于2026-11-06核对，只借鉴研究和数据处理原则，不增加Qlib、LEAN或其他第三方运行依赖：

- [Qlib官方PIT说明](https://qlib.readthedocs.io/en/latest/advanced/PIT.html)：财报的报告期与发布时间不同，同一指标会有多个修订版本。当前程序已有公告截止筛选、原始记录缓存和冲突阻断；尚未取得完整历史公告版本档案。今天取数时的 `fetched_at` 只证明本次获取时间，不能证明值在历史决策日已经存在。
- [QuantConnect Research Guide](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/research-guide)：说明前视偏差、幸存者偏差、复权价格使用和按时间推进的验证。项目以原价成交加现金派息、独立历史证券集合及次日执行减少相关问题；动态重建池、停牌估值、退市退出和完整公司行动尚未完成，不能宣称已消除这些偏差。
- [Bailey、Borwein、López de Prado、Zhu：The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)，2015-02修订版：第4—7页讨论反复试验及普通留出法的局限，第23—24页说明试验记录完整性与输入假设的重要性。项目保留协议和全部4%/5%/6%试验，不按10%目标寻找历史优胜参数；尚未实现论文的PBO/CSCV检验，年度或重叠三年窗口统计也不等于该检验。

600882.SH在2014-12-31的接口行业标签为食品饮料，但公司当时已经披露的业务为矿业，证据记录在 `qmt/disclosures.json` 的 `industry_conflicts`：

- [华联矿业2013年年报原件（公司公告镜像）](https://file.finance.sina.com.cn/211.154.219.97%3A9494/MRGG/CNSESH_STOCK/2014/2014-4/2014-04-28/1390444.PDF)，2014-04-28公告，PDF第6页：经营范围经历2012年矿山投资及2013年矿石销售、矿山设备、矿业开发和技术服务等变更。这份资料在2014-12-31前已公开。
- [华联矿业关于拟变更公司名称及简称的公告](https://static.cninfo.com.cn/finalpage/2016-04-26/1202246074.PDF)，2016-04-26公告，PDF第1页：拟置出矿业相关股权并置入乳品公司，置换完成后转型乳制品。该公告只作事后交叉核验，不能回填为2014年的决策依据。
- 现有证据足以否定该条食品饮料标签用于2014年同行估值，不能据此认定整个接口都在回填当前分类，也不能凭主营描述猜测当时申万代码。保留该股和全部20股的数据准备；回放区间覆盖已登记冲突日时阻断策略收益，ETF/现金参考仍可独立计算。

当前短期入口版本为 `historical-dividend-v7`，长期入口 `python -X utf8 -m qmt backtest --long-history` 使用 `historical-dividend-v7-long`，均在 `data/capital-100k/backtests/` 下以10万元、最多20只持仓的新配置独立登记。长期在2014-12-31建池，从2009年取财务、2012年取估值预热，目标回放2015年至可用完整交易日；开发段截至2019年、留出段从2020年开始。既往已查看部分历史结果，留出段不再完全未见；年度和以月末及最终截至日结束的滚动三年统计只分析固定曲线，没有逐窗重新建池或训练。原 `data/backtests/` 中的v2/v3短期诊断仍为旧1万元、最多5只持仓、单股2,000元及行业4,000元上限，原结果不重写、不外推为新配置收益。v4/v5/v6长期准备中断保留，不能记作完成回测。

2026-11-06已核对v7-long实际文件：`market.json` 为20股、2014-12-31至2026-11-06共2,878个交易日、56,773条日线；`valuation-history.json` 共71,186条估值记录；`financials.json` 六类接口合计10,180条财务/分红条目。取数、诊断和报告渲染完成，但 `backtests/latest.json` 中三组策略均为 `blocked`，原因是600882历史行业原文冲突，以及601088同一分红事件冲突；未生成策略收益。现金与510880复权参考独立成功，后者同期年化约6.34%、最大回撤约-46.56%，仍是未扣投资者费用的参考口径。新10万元日常快照评估154家公司、合格候选0只、订单及成交均为0；这些结果不证明年化目标实现。

## 长期红利 ETF 比较基准

- [华泰柏瑞上证红利 ETF 产品资料概要（上交所披露，2020-11-20）](https://www.sse.com.cn/disclosure/fund/announcement/c/2020-11-20/510880_20201120_1.pdf)：代码510880，基金合同生效日2006-11-17，上交所上市日2007-01-18，跟踪上证红利指数。因此可用于2014年底起的长期参考；它与515180易方达中证红利ETF是不同基金，不拼接或替换旧实验的基准结果。
- [Tushare ETF日线](https://tushare.pro/document/2?doc_id=127)单次最多5000条；[基金复权因子](https://tushare.pro/document/2?doc_id=199)单次最多2000条。长期参考按至多1500个交易日分批、共享一个边界交易日衔接归一结果，每批都要求与相同交易日历逐日一致；缺失或上市前窗口不填旧价、不缩短时间。
- ETF参考仍为收盘价乘复权因子的连续归一曲线，未扣投资者佣金、滑点或个人税费；不另加现金分红，不记为模拟成交或可执行净收益。
