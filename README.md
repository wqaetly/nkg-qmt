# NKG AI 股票研究

由 AI Agent Skill 驱动的股票与 ETF 分析工具：**Tushare/Fuyao 取数与筛选 → Gemini 网页 Deep Research → Codex 核验、复算与研究判断 → 人工操作**。

以数月至更久的中长期持有为主，早午盘报告帮助跟踪证据、估值与投资论点的变化。研究可以得出“今天无需操作”；没有固定收益目标、机械买卖策略、回测引擎或交易账本。

## 研究入口

| Skill | 任务 |
| --- | --- |
| [watchlist-review](skills/watchlist-review/SKILL.md) | 本地自选股与真实持仓快照、逐项复核、组合风险及相对上次的建议变化 |
| [market-session-research](skills/market-session-research/SKILL.md) | 候选筛选、股票与 ETF 对比、持仓复核；编排数据、Gemini 深研与 Codex 复核，默认北京时间约 10:00、14:00 出报告 |
| [dividend-stock-research](skills/dividend-stock-research/SKILL.md) | 核验业务、分红可持续性、现金流与债务，以悲观/基准/乐观情景比较股息参考价 |

红利研究是分析方法，股息计算器只做假设下的算术校验，不产生交易信号。Agent 根据本次任务决定研究范围、数据需求和待验证问题，不使用预设必买名单或固定仓位分配。

## 安装研究技能

可以直接让 Codex 读取仓库内的 `SKILL.md`。也可以把 `skills/` 下的三个目录复制到个人技能目录（默认 `~/.codex/skills`；自定义 `CODEX_HOME` 时为其 `skills` 子目录）。已有同名技能时，先检查并保留个人修改，再更新文件。

安装后调用示例：

```text
使用 $watchlist-review，复核当前全部持仓和自选股。
逐项说明上次结论与本次结论、改变依据、操作条件和待核验问题。

使用 $market-session-research，做今天的早盘研究，中长期持有。
先筛选值得深查的股票或 ETF，让 Gemini 研究，再由 Codex 核验并给出条件性结论。

使用 $dividend-stock-research，分析指定公司的分红可持续性。
列出支持与反对证据，用三种情景计算参考价格，并说明哪些事实会推翻结论。
```

涉及真实组合时提供最新持仓、币种和资金约束；旧聊天或旧截图不会自动视为当前持仓。默认只向 Gemini 发送公开研究信息，私人账户信息留在本地处理。

## 自选股与持仓

当前先手动运行，流程跑通后再启用定时。截图或用户确认的数据存到 `data/watchlist/snapshots/`，最近一份为 `data/watchlist/current.json`；[快照格式](skills/watchlist-review/references/snapshot-format.md) 区分已持有与仅关注、账户、市场、币种和数据时间。真实快照不会进入 Git。

准备与算术检查（不联网、不生成投资建议）：

```powershell
python -X utf8 skills/watchlist-review/scripts/prepare_watchlist.py
```

脚本生成公开证券名单和仅本地的核对结果，按账户与币种分组，不把购买力当现金，也不把截图价格当实时行情。完整复核由 `$watchlist-review` 接着取数、调用 Gemini、由 Codex 本地结合组合给出结论；每个持仓都须有状态。新交易由用户确认后更新快照，不根据建议自动改仓。日报记录“维持/改变/新增”及证据，支持下一次继续跟踪。

## 数据与浏览器

- **Tushare**：使用已配置的 MCP，或本库 `qmt.market.Tushare` HTTP 客户端。环境变量为 `TUSHARE_TOKEN` 和可选的 `TUSHARE_BASE_URL`。
- **Fuyao**：按 [数据接入说明](skills/market-session-research/references/data-sources.md) 调用官方数据接口，凭据为 `FUYAO_API_KEY`。它与 Tushare 的字段、单位和覆盖范围不同，不能只替换服务地址。
- **Gemini**：连接用户指定、已登录的 Chrome 配置，通过网页 Deep Research 生成研究。浏览器直连，不依赖桌面点击或 Gemini API。
- **Codex**：读取原始公告与基金文件、复算关键数字、处理来源冲突，说明最终采纳或修正了什么。

凭据可放在被 Git 忽略的本地 `.env`，由 `qmt.config.load_env()` 加载。首次配置可参考 [.env.example](.env.example)，不要覆盖已有 `.env`。安装 skill 不会配置密钥、开通数据权限或创建定时任务。

`qmt/` 仅保留 Python 3.10+ 标准库实现的只读数据助手：

| 入口 | 用途 |
| --- | --- |
| `Tushare.call(api, params, fields)` | 结构化数据查询，校验响应与满页风险 |
| `Tushare.calendar / basics / prices` | 交易日历、股票身份、指定日期日频估值 |
| `Tushare.candles / bars` | 历史 K 线及涨跌停数据，不生成买卖规则 |
| `Tushare.dossier` | 财务、分红与审计底稿，逐项记录数据缺口 |
| `request_json / load_env / atomic_json` | HTTPS 请求、加载本地凭据、保存研究数据 |

保留 `qmt` 包名是为了复用这些接口；不再提供 `python -m qmt daily/backtest/plan` 或 `start.ps1` 入口。

## 报告与验证

报告和研究包保存到被忽略的 `data/agent-research/YYYY-MM-DD/HHmmss/`。报告应包含信息截止时间、行情时间、证据来源、情景假设、条件性结论、反证和下次复核点。午盘重点解释相对早盘的变化。数据不足、Gemini 未完成或关键事实冲突时，明确标注待核实。

离线验证命令（仓库根目录）：

```powershell
python -X utf8 -m unittest discover -s tests -v
Push-Location skills/dividend-stock-research
python -X utf8 -m unittest discover -s tests -v
Pop-Location
```

这些测试只验证数据处理与算术，不证明投资预测或收益。完整网页研究仍受登录状态、账户额度和研究耗时影响；10:00/14:00 是目标交付时间，尚未配置自动运行。

## 来源与数据边界

红利技能从 [dividend-stock-research](https://github.com/wqaetly/dividend-stock-research) 导入，保留其 [MIT 许可证](skills/dividend-stock-research/LICENSE) 和 [来源记录](skills/dividend-stock-research/UPSTREAM.md)。其余数据口径见 [UPSTREAM.md](UPSTREAM.md)。

密钥、行情数据、私人聊天、持仓和研究报告不提交到 Git。旧回测与模拟代码已从当前版本移除；历史提交仍可追溯。已有本地 `data/` 文件未清除，当前 skill 只使用本次研究包和明确指定的历史研究材料，不自动读取旧模拟账本或实验结果。
