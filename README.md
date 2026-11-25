# 撤稿预警 · 综述引文失据推演

编辑收到撤稿预警后，判断综述中最多还有多少结论会失去引文支持的 Web 应用。

## 业务规则

- 一条结论由一个或多个**引文组**支撑，只要还有一组完整（组内无撤稿文献），结论就成立。
- 引文组中**任一**文献撤稿，该组即失效；结论的所有引文组都失效时，该结论**失据**。
- 文献可标记为：**正常 / 预警 / 已撤稿**；预警文献最多 **20 篇**。
- 推演时输入「可能新增撤稿的篇数上限 k」，系统在预警文献中选出不超过 k 篇的一组 DOI，
  使其（假设撤稿后）能让**最多的其他结论新增失据**，并列出：
  1. 启动快照下已经失据的结论；
  2. 最优预警 DOI 组合；
  3. 受该组合影响而新增失据的结论及条数。
- 推演只使用**启动时刻的标记快照**：推演期间标记仍可修改，已完成结果若与当前标记/数据
  不一致会明确标注「**已过期**」。
- 推演失败时在结果区提示错误，并保留上一次成功结果；失败记录单独入库，不会覆盖旧结果。

## 技术栈

Flask + SQLite（标准库 `sqlite3`）+ 原生 HTML/JS，Gunicorn 单进程多线程运行，
Docker Compose 一键启动。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据文件持久化在宿主机 `./data/app.db`（compose 已挂载卷）。

本地不使用 Docker 时：

```bash
pip install -r requirements.txt
DB_PATH=app.db gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 \
    "app:create_app()"
```

> 必须使用单 worker（多线程），推演任务在进程内后台线程运行并依赖线程锁。

## CSV 格式

UTF-8（或 GB18030）编码，表头支持中英文，必需三列：

| 结论号 | 引文组号 | DOI |
---|---|---
| 结论号 / conclusion_id | 引文组号 / group_id | DOI / doi |

示例见 [`examples/sample.csv`](examples/sample.csv)。DOI 匹配不区分大小写；
完全重复的行会自动去重。重新上传会替换数据集，但已保存的同 DOI 标记会保留。

## 算法说明

- 已失据：结论的每个引文组都与「已撤稿」DOI 集合有交集。
- 可推演（killable）：当前未失据，且每个仍然完整的引文组都至少包含一篇预警文献。
- 优化目标：在 ≤k 篇预警中选子集，使新增失据结论数最大。预警上限 20，采用精确枚举
  （最坏 C(20,10)=184,756 次），条件以整数位掩码编码；同分时按 DOI 字典序取最小组合，
  结果确定可复现。典型数据量下秒级返回，最坏构造约 10 秒，在后台线程执行不阻塞页面。

## 接口

| 方法 | 路径 | 说明 |
---|---|---
| GET  | `/api/state` | 数据集、文献标记、计数、实时失据结论、最近一次推演 |
| POST | `/api/dataset` | 上传 CSV（multipart 字段 `file`），替换数据集 |
| POST | `/api/papers/mark` | JSON `{doi, mark}` 修改标记；预警超额返回 409 |
| POST | `/api/simulate` | JSON `{k}` 启动后台推演 |
| GET  | `/api/simulations/latest` | 最近一次推演（含 `stale` 过期标记） |

## 测试

```bash
# 纯标准库可运行，无需安装 Flask
python3 -I tests/test_solver.py        # 算法与 CSV 解析单元测试（14 项）
python3 -I tests/test_integration.py   # SQLite 全流程集成测试（22 项）
python3 -I tests/test_routes_shim.py   # 路由契约测试（内置 Flask 垫片）
```
