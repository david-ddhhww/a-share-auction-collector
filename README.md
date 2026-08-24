# A股集合竞价数据采集器

轻量级 Python 采集器，用于保存 A 股 9:25 最终开盘集合竞价的个股明细、四板汇总、全 A 汇总、Top20、行业汇总、质量报告和历史 SQLite 数据。

## 已确认的数据口径

东方财富 `trends2/get` 的源时间标签与逻辑成交时间不同：

- 09:15–09:25 行是虚拟撮合价格，成交量/额为 0。
- 09:25 最终撮合的真实量额写在 **09:26 行**。
- 竞价价取 `f53`，竞价量取 `f56`（手），竞价额必须直接取 `f57`（元）。
- 09:30 行量额仍为 0；09:31 才是连续竞价首分钟，绝不能混入。
- 数据库同时保存 `logical_auction_time=09:25` 和真实 `source_timestamp=09:26`。

2026-08-24 已完成上海主板、深圳主板、创业板、科创板各 5 只，共 20 只交叉验证，20/20 与独立逐笔 09:25 最终成交匹配。详见 [字段验证报告](docs/字段验证报告.md)。

## 两道生产门槛

1. 字段语义门槛：已通过。
2. 全市场批量快照门槛：待下一个交易日 09:26 实盘验证。

全市场逐股请求约 5000 次容易触发上游断连，不能可靠保证 09:30 前完成。程序默认在 09:26:05–09:28:59 获取东方财富 `clist/get` 四板分页快照，再用上述 20 只 `trends2 09:26/f57` 做同时点校验。只有 20/20 金额一致才接受批量快照；否则拒绝信任快照，降级逐股采集并将不完整报告标为 `poor`。

## 安装

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## 运行

定时任务在中国时区 09:26 启动：

```bash
auction-collector --root /home/ubuntu/a-share-auction-collector --mode auto
```

仅用于最近交易日的盘后逐股补采（不使用已经包含全天成交的批量快照）：

```bash
auction-collector --root /home/ubuntu/a-share-auction-collector --mode per-stock --allow-catchup
```

限制并发：

```bash
auction-collector --root /home/ubuntu/a-share-auction-collector --mode per-stock --concurrency 4
```

## 每日输出

```text
data/YYYY-MM-DD/
  stock_auction.csv
  board_summary.csv
  top20.csv
  industry_summary.csv
  report.json
  quality.json
database/auction_data.db
raw/YYYY-MM-DD/
logs/YYYY-MM-DD.log
```

`report.json` 是下游 AI 生成竞价快报的统一入口。原始响应不可变保存；同日同股票使用 SQLite 主键幂等覆盖，不重复计数。

## 零成本云端运行

公开仓库：<https://github.com/Kevinhuang287/a-share-auction-collector>

仓库内置 `.github/workflows/daily-auction.yml`。GitHub Actions 在工作日北京时间 09:10、09:15、09:20、09:25 发起冗余调度，真正采集只允许发生在已经验证的 09:26:05–09:28:59 窗口。首个任务会等待窗口，后续任务发现当日已有合格报告后直接退出。

云端入口：

```bash
auction-cloud-run --root .
```

只有质量为 `good` 或 `warning`、有效股票数和全 A 竞价额均大于零的报告才会更新 `latest/`。`poor`、空数据、节假日或错过窗口时任务失败或跳过，绝不覆盖上一份成功报告。GitHub Actions 仅提交 `data/`、`database/` 与 `latest/`；原始响应、日志和临时状态不进入公开仓库。

公开网站读取稳定地址：

```text
https://raw.githubusercontent.com/Kevinhuang287/a-share-auction-collector/main/latest/report.json
```

首次实盘任务运行前，该文件不存在是正常状态，网站必须显示“等待首份合格报告”，不得填模拟值。

## 以后迁移到 Linux 服务器

迁移时不改采集算法、SQLite 表、每日输出或 `latest/report.json` 契约，只把 GitHub Actions 替换为仓库已提供的 systemd timer 或 cron：

1. 把仓库与 `data/`、`database/` 复制到服务器。
2. 安装 Python 3.11 和项目依赖。
3. 让 systemd/cron 在 Asia/Shanghai 09:26 调用 `auction-cloud-run --root <项目目录>`。
4. 用 Nginx、对象存储或仓库同步继续公开 `latest/report.json`。
5. 网站只需把报告 URL 改到新地址；数据字段和页面无需重写。

GitHub Actions 的定时调度不是严格实时 SLA，可能被平台延迟；服务器迁移路线主要用于需要更稳定 09:26 启动时间的阶段。

## 质量和停止规则

- 缺失率 `<1%` 为 `good`，`1%–3%` 为 `warning`，`>3%` 为 `poor`。
- 缺失、停牌或零成交未确认时保存 `NULL`，绝不拿 0 冒充有效数据。
- 负成交额、重复股票代码、日期不匹配、缺少 09:26 行都会阻断或标记失败。
- 四板合计与全 A 由同一批有效个股计算，避免口径漂移。
- 至少 5 个历史交易日后，全 A 竞价额若低于历史中位数 0.2 倍或高于 5 倍，标记 `poor`。
- 上游暂时错误最多重试 2 次；并发范围限制为 1–12，默认 6。
- `poor` 报告明确写入“不建议直接用于交易判断”。

## 测试

```bash
python -m pytest -q --basetemp=.pytest-tmp
python -m compileall -q src
```

## systemd 部署

模板位于 `deploy/systemd/`。服务器需先恢复 SSH，随后按 [部署说明](docs/部署说明.md) 安装。定时器故意设置 `Persistent=false`，避免机器在盘后恢复时误用全天累计成交额补跑批量快照。
