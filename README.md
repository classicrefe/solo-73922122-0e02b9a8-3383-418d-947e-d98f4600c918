# 风电限发损失归因入仓

Flask + SQLite 网页应用：导入一台风机的 15 分钟计量表（UTC 时刻、可发量、实发量），
录入/更正限发单，按日核算各原因损失与“待查”电量。

## 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>。SQLite 数据存放在命名卷 `curtailment-data`
（容器内 `/app/instance/curtailment.sqlite3`），重启容器不丢数据。

本地不用 Docker 时：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 'curtailment.app:create_app()'
# 或 python -m curtailment（Flask 开发服务器，勿用于生产）
```

环境变量：`CURTAILMENT_DB`（数据库路径）、`CURTAILMENT_SECRET`（会话密钥，部署前务必修改）、
`HOST`/`PORT`（`python -m curtailment` 时生效）。

## 计量表 CSV

带表头（推荐），列名可用中文或英文：

```csv
时刻,可发量,实发量
2026-01-01T00:00:00Z,10.0,4.0
2026-01-01T00:15:00Z,12.5,4.0
```

- 时刻用 ISO 8601；带时区的会换算到 UTC，不带的按 UTC。必须对齐 15 分钟。
- 可识别列名：`时刻/时间/utc时刻/time/timestamp`、`可发量/可发/理论可发量/available`、
  `实发量/实发/实际发电量/actual`，可另加 `风机/机组/turbine` 列。
- 无表头的三列文件按 `时刻, 可发量, 实发量` 解析（四列时第 4 列为风机）。
- 一个文件只能是一台风机；重复导入同一 (风机, 时刻) 会覆盖原值。
- `实发量 > 可发量` 的格会提示并按无损失处理。

## 规则

1. 计量表按 15 分钟一格；单据覆盖满足 `开始 <= 格时刻 < 结束`（结束为开区间）。
2. 同一格同一原因有多张单时，取**最低上限**；不同原因各自保留最低上限。
3. 在 `[实发量, 可发量]` 之间用该格所有上限切段；每一段均分给
   “上限**不高于该段下端**”的所有原因（每个原因每段至多一份）。
4. 没有原因认领的段计入**待查**，页面按日列出待查电量与待查格。
5. 任一覆盖格 `实发量 > 每格上限` 即冲突：**整单退回**，页面列出冲突格与单号，
   提交/更正失败时原单据与归因结果都保持不变。
6. 单据提交（含同号更正）与归因结果在同一事务里生效；更正只重算**新旧单覆盖的日期**，
   重算时其他限发单（含重叠的有效更正）一并参与，因此重叠单据都会计入。
7. 结果量化到 0.001 kWh；每格保证 `各原因损失 + 待查 = 可发量 − 实发量`（最大余数法分摊尾差）。

导入新计量后，若既有单据出现冲突（实发量超上限），该单会被整单暂停并在首页/日报显著提示，
其覆盖的损失落为待查；重新导入合规数据或更正单据后自动恢复。

## 目录

- `curtailment/allocation.py` — 纯计算：分段、归因、冲突检查
- `curtailment/parsing.py` — 时刻/CSV 解析
- `curtailment/service.py` — 导入、单据、重算、查询
- `curtailment/app.py` — Flask 路由
- `templates/`, `static/` — 页面
- `samples/` — 示例计量 CSV
- `tests/` — pytest（55 项）

限发单没有风机字段，按**全风场口径**处理：一张单对其覆盖时段内所有风机的计量格都生效；
冲突检查也针对这些格。

## 测试

```bash
pip install -r requirements.txt pytest
python -m pytest
```
