# 来源与改写说明

## 合并到 NKG AI 量化研究库

- 直接来源：https://github.com/wqaetly/dividend-stock-research
- 合并版本：`1cd5475314fefdcedb9fcf7bcc4c3fe4fd1f8e41`，2026-11-10 核验为其 main 最新提交。
- 导入该版本全部文件，包括技能、方法说明、计算脚本、测试、MIT 许可证及原始来源说明；以文件快照纳入本库，原仓库 Git 历史仍留在源仓库。
- 本库调整：增加与早午盘研究技能的协作说明、Tushare HTTP/Fuyao 接入路径，取消固定 MCP 元数据依赖，修正合并后的安装路径。计算脚本与测试未改动。
- `vendor/dividend_stock_research` 是旧回测使用的冻结副本；保留原路径和内容以维持导入兼容与实验代码指纹。新的技能维护以 `skills/dividend-stock-research` 为入口，不反向改写已封存实验。

## 原始来源

- 上游：https://github.com/a985987819/dividend-yield-analysis
- 参考版本：`b242d7acace0808e4efa6c1645f9862f8b7698d0`
- 参考日期：2026-10-25
- 参考文件：README.md、SKILL.md、CHANGELOG.md、LICENSE。
- 许可：MIT，原始版权声明保留在本仓库 LICENSE。

上游仓库介绍称其思路来自 B 站 UP 主“史诗级韭菜”。本项目未独立核对全部原视频，也不代表该 UP 主或上游作者的认可；后续添加和修正由本项目负责。

保留股息率倒推买价、多年财务核查和情景比较的思路，重写筛选过程及边界。修正经营现金流替换利润、固定70%支付率、将 ROIC 判定等同于分红保证、系数方向不一致等问题。加入停派、政策条件、最新财报、特殊行业、证据缺口和组合约束；移除强制 HTML 与固定通胀假设。上游案例中的公司财务数值没有作为已验证数据复制进本项目。

可复核原文：https://github.com/a985987819/dividend-yield-analysis/tree/b242d7acace0808e4efa6c1645f9862f8b7698d0
