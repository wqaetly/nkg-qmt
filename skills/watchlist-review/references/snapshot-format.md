# 本地快照格式

`data/watchlist/current.json` 是最近一次用户提供的组合快照，不是实时券商账户。使用 UTF-8 JSON；金额、数量和价格用十进制字符串，未知值用 null，禁止用 0 代替未知。每次更新先保存一份带时间的不可覆盖快照。

最小结构示例只用虚构数据：

```json
{
  "schema_version": 1,
  "captured_at": "2026-01-05T15:20:00+08:00",
  "accounts": [
    {
      "id": "account-a",
      "summaries": [{"currency": "CNY", "market_value": "1000", "cash_available": null}]
    }
  ],
  "entries": [
    {
      "kind": "holding",
      "account_id": "account-a",
      "symbol": "000001.SZ",
      "name": "格式演示，非真实持仓",
      "market": "CN",
      "currency": "CNY",
      "asset_type": "stock",
      "identity_status": "unverified",
      "position": {
        "quantity": "100",
        "available_quantity": null,
        "cost_price": "11",
        "snapshot_price": "10",
        "market_value": "1000",
        "reported_pnl": "-100",
        "quote_at": null
      }
    }
  ]
}
```

- `market` 为 `CN/HK/US`；代码分别为六位加 `.SH/.SZ/.BJ`、五位加 `.HK`、美股代码（保留类别后缀）。来源接口代码可能不同，调用时显式转换，不修改原始标识。
- `identity_status` 为 `verified/unverified`。`unverified` 可保留 null 代码，待身份确认；脚本不会将其放进公开研究名单。
- `kind` 为 `holding/watch`。`holding` 必须对应存在的账户并提供大于零的数量；`watch` 不提供 `position` 或 `account_id`。当前仅支持普通多头股票、ETF、ADR，不把空头、衍生品或负数量当普通持仓。
- `asset_type` 为 `stock/etf/adr`。账户、代码和币种共同标识一笔持仓；同标的跨账户可以共存。同市场同代码的名称/币种/类别冲突需先核对。
- `accounts[].summaries` 按币种分组，允许记录 `net_assets/market_value/cash_available/reported_pnl/daily_pnl`。CNH 折算净值与 HKD/USD 原币资产分开，不相加。未显示现金不从购买力或净值差额反推。
- `source_files`、`identity_source`、`notes`、`thesis`、`review_triggers`、`max_buying_power`、`remaining_liquidity` 可作为仅本地的来源/研究备注。额外字段不进入公开研究名单；不要录入账号、密钥或登录资料。
- `captured_at` 必须带时区，代表截图或用户确认时间；`quote_at` 是单个证券明确显示的行情时间，没有就填 null。以文件名日期和截图时钟推定采集时间时，在 notes 中记录推定依据。

运行准备脚本后，`data/watchlist/prepared/时间/` 中生成：
- `public-research.json`：仅证券代码、名称、市场、币种、类别；身份未确认的标的不会发送，仍须在最终报告列为待核验。
- `checks.json`：私有检查结果、同账户同币种可见持仓市值与权重、对账差额及未知项。权重分母是该组可见且完整的市值，不是全账户净资产，也不是建议目标仓位。

脚本不联网、不更新行情、不模拟成交、不计算投资建议。它不能证明截图覆盖全部账户，也不校正券商盈亏。差额是需要解释的证据，不自动推断为费用、漏仓或行情延迟。
