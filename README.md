# 用户标签画像与行为分析平台

面向用户行为埋点采集、标签体系构建、人群圈选与行为特征分析的数据应用平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 事件埋点 | `event_track` | 埋点事件 | 事件编号、事件名称、事件分类 |
| 标签定义 | `tag_define` | 标签规则 | 标签编号、标签名称、标签层级 |
| 标签计算 | `tag_compute` | 计算任务 | 任务编号、关联标签、计算范围 |
| 人群圈选 | `crowd_select` | 圈选条件 | 条件编号、条件名称、标签组合 |
| 行为路径 | `behavior_path` | 路径模型 | 模型编号、起始事件、目标事件 |
| 留存计算 | `retention_calc` | 留存配置 | 配置编号、初始行为、回访行为 |
| 用户分群 | `user_segment` | 分群定义 | 分群编号、分群名称、基准行为 |
| 生命周期价值 | `lifetime_value` | LTV模型 | 模型编号、用户分群、价值指标 |
| 漏斗分析 | `funnel_analyze` | 漏斗定义 | 漏斗编号、漏斗名称、步骤序列 |
| 事件清洗 | `event_cleanse` | 清洗规则 | 规则编号、事件名称、校验字段 |
| 会话切割 | `session_split` | 切割策略 | 策略编号、策略名称、超时阈值 |
| 画像卡片 | `profile_card` | 卡片配置 | 卡片编号、卡片名称、展示标签 |
| 属性字典 | `attr_dict` | 属性定义 | 属性编号、属性名称、数据类型 |
| 交集分析 | `overlap_analysis` | 交集任务 | 任务编号、人群A、人群B |
| 标签下钻 | `tag_drill` | 下钻配置 | 配置编号、父标签、子标签集 |
| 事件聚合 | `event_aggregate` | 聚合配置 | 配置编号、事件名称、聚合维度 |
| 标签快照 | `tag_snapshot` | 快照记录 | 快照编号、快照日期、用户基数 |
| 行为热力 | `behavior_heatmap` | 热力配置 | 配置编号、行为事件、时间范围 |

## 标签结果数据（第 2 版）

标签计算结果有两套版本，**同一个条件只能用一个版本解释**：

| 版本 | 产物类型 | 语义 |
| --- | --- | --- |
| 第 1 版 | `tag_compute`（`result_version=1`） | 布尔明细，靠“有没有这一行”表达命中，只用于旧活动名单回放 |
| 第 2 版 | `tag_result_v2` | 每个会员一条记录，`computed` / `hit` / `tag_value` 把“未计算 / 未命中 / 已计算”拆开 |

第 2 版批次头部字段：

- `value_type`：该批次实际的值类型（`bool` / `int` / `float` / `string`），圈选按它比较字段语义；
- `coverage`：`explicit` 记录了本次计算的全体会员、`hit_only` 只留命中者、`unknown` 覆盖方式未知；
- `miss_covered`：是否拥有可靠的未命中集合。`hit_only` / `unknown` 一律为 `false`；
- `source_version`：`2` 为原生第 2 版，`1` 为兼容迁移而来，`migrated_from` 记录来源版本；
- `migration_issues`：迁移时无法判定的会员（值缺失、`unknown`、意外未命中行等）。

兼容迁移只转换旧结果真正记录过的信息：`explicit` 批次的非命中行迁为未命中；
`hit_only` 批次只迁命中者，**未出现的会员保持未知，不会补成未命中**；
旧行值缺失/`unknown` 只登记问题，不迁为未命中。仅命中批次上的否定条件（`NOT`、`eq false`）
会判为“不可判定”，不给名单。

### 兼容上线接口

```bash
# 1) 查看待迁移的旧结果
curl http://127.0.0.1:8000/api/crowd-select/tag-results/migration

# 2) 执行兼容迁移（interrupt_after 可在第 N 个批次后模拟中断）
curl -X POST http://127.0.0.1:8000/api/crowd-select/tag-results/migrate
curl -X POST 'http://127.0.0.1:8000/api/crowd-select/tag-results/migrate?interrupt_after=1'

# 3) 切换圈选读取的结果版本（旧结果未迁完时切 v2 会返回 409）
curl -X POST 'http://127.0.0.1:8000/api/crowd-select/tag-results/switch?version=v2'

# 4) 新名单按第 2 版圈选；旧活动名单固定按第 1 版回放
curl -X POST http://127.0.0.1:8000/api/crowd-select/CS-001/run
curl -X POST http://127.0.0.1:8000/api/crowd-select/CS-001/replay

# 5) 在验证环境同时跑新旧两版并核对名单 / 版本 / 覆盖语义
curl http://127.0.0.1:8000/api/crowd-select/CS-001/compare
```

迁移按确定性批次编号 `TRB-<task_id>-V2` 写入：中断后重新进入会续跑，不会产生重复批次。
`backend/scripts/verify_tag_v2.py` 把上述核对串成一条命令：

```bash
cd backend && .venv/bin/python scripts/verify_tag_v2.py --base http://127.0.0.1:8000
```


## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
