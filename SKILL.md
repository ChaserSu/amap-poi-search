---
name: amap-poi-search
description: 用高德 Web 服务 API 全量抓取指定项目周边或指定行政区内的 POI（如餐饮业态），四叉树网格突破单请求 200 条上限，输出含三级业态分类和到项目直线距离的 CSV。当用户要求「某项目周围 N 公里的餐饮/店铺/业态」「某行政区内所有餐饮/商家」「高德 POI 搜索/导出 CSV」时使用。不用于飞书表格写入、路径规划或地图可视化。
---

# amap-poi-search

基于高德「Web服务」REST API 的 POI 全量抓取技能。产物只有 **CSV**，不写飞书。

脚本入口：`scripts/amap_poi.py`（纯 Python 标准库，无需 pip 安装）。

## 第 0 步：检查高德 MCP 与 API Key

按顺序执行，不要跳过：

1. **找 MCP 配置文件**（key 就藏在 MCP URL 的 `?key=` 里）：
   - TRAE CN（IDE）：`~/Library/Application Support/Trae CN/User/mcp.json`
   - TRAE SOLO CN：`~/Library/Application Support/TRAE SOLO CN/User/mcp.json`
   - 国际版把 `Trae CN` 换成 `Trae`
   - 若存在 server 名 `amap-maps-streamableHTTP`，从其 `url`
     （形如 `https://mcp.amap.com/mcp?key=<KEY>`）解析出 `<KEY>`。
2. **没有该 MCP**：引导用户在 TRAE 的 MCP 设置中添加，URL 填
   `https://mcp.amap.com/mcp?key=<KEY>`（此时还没有 key，进入第 3 步）。
3. **引导申请 Key**（用户无 key 时必须完整说明）：
   - 打开 https://console.amap.com 注册登录
   - 「应用管理」→「创建新应用」→「添加 Key」
   - **服务平台必须选「Web服务」**（选错会报 `USERKEY_PLAT_NOMATCH / 10009`）
   - 拿到 Key 后：填入 MCP URL 完成 MCP 配置；同时在本技能中使用
4. 调用脚本时用 `AMAP_KEY` 环境变量或 `--key` 传入。
   **绝不要把 Key 硬编码进任何文件、不要提交到 Git、日志中注意脱敏。**
5. **域名选择（官方优先）**：脚本默认请求高德官方 `https://restapi.amap.com`，
   可用环境变量 `AMAP_BASE` 覆盖。
   - 默认：官方域名 + 官方 Key（含从 MCP URL 解析出的 Key），无需额外设置
   - **仅当用户显式提供自己的中转站域名与配套 Key 时**，才设置
     `export AMAP_BASE="用户的中转域名"` 并使用中转 Key
   - 绝不主动建议或默认使用任何第三方中转站；Key 与域名绑定、不可混用

说明：MCP 自身工具默认 `extensions=base` 且无法传参，**拿不到电话**，所以本技能
绕过 MCP 直接调 REST API。

## 第 1 步：向用户确认模式与参数

告知用户有两种模式（对应两种算法），确认后开工：

| 模式 | 典型说法 | 必填参数 |
|---|---|---|
| **circle 圆形** | 凤城悦天地周围 50km 的所有餐饮 | 项目真实坐标、半径(米)、types |
| **district 行政区** | 凤城悦天地所在的海陵区所有餐饮 | 行政区 adcode、types |

`types` 默认 `050000`（全部餐饮服务，含中餐/快餐/火锅/咖啡/糕饼等全部子类）；
需要更窄业态时用高德 6 位分类码，多个用 `|` 拼接。

## 第 2 步：精确定位项目（两种模式都必须做）

```bash
python3 scripts/amap_poi.py locate --project "凤城悦天地" --city 泰州
```

从候选列表中**核对名称与地址**，取真实坐标，例如
`119.922427,32.435380`。district 模式还需行政区 adcode（如海陵区 `321202`，
可让用户确认，或从候选 POI 的行政区信息推断）。

**铁律：禁止直接用地理编码定位项目。** 地理编码会把项目名静默降级解析为
"区/市行政区中心点"（返回 `level=区县`），坐标可能偏差数公里。详见
[references/pitfalls.md](references/pitfalls.md)。

## 第 3 步：全量抓取

**圆形模式**（四叉树 + 圆形剪枝；用小矩形拼出完整圆）：

```bash
AMAP_KEY=<KEY> python3 scripts/amap_poi.py circle \
  --project-lon 119.922427 --project-lat 32.435380 \
  --radius 50000 --types 050000 \
  --workdir ./tiles_circle --raw-out raw_circle.csv
```

**行政区模式**（边界本地化：只发 2 顶点小矩形，区划边界不发给 API，
点-多边形判定全部在本地，避免 790 顶点导致的 413 URL 过长）：

```bash
AMAP_KEY=<KEY> python3 scripts/amap_poi.py district \
  --district 321202 --types 050000 \
  --workdir ./tiles_district --raw-out raw_district.csv
```

执行规则：

- **三条防烧钱防护已内置，必须使用新版脚本**（见 pitfalls #16，曾因调用量失控产生高额账单）：
  1. 开跑前自动探测并打印「预计总请求数」（≤5 次探测调用，且探测页会被正式抓取复用）；
     估算值过高时必须先告知用户、经确认再继续，或改用更小半径/行政区模式；
  2. 每个 HTTP 请求实时打印 `[请求#N]` 日志，发现请求数异常增长立即让用户中止；
  3. 每翻完一页立即原子落盘到 `workdir/pages/`，随时 Ctrl+C 只损失当前 1 次请求，
     重跑从断点页继续、已抓页 0 请求。
- **关键词补全绝不能各自跑四叉树**：宽口径场景以官方 types 分类码的四叉树为全量主力，
  关键词只用全市文本搜索翻页（每词封顶 8 次调用）或浅格补漏，否则调用量 ×关键词数 爆炸。
- workdir 是断点续传缓存，**任务再慢也不要删**；中断后原样重跑即可续传，
  已抓的页/格子（pages、leaf/split 标记）不会重复请求。
- **每个搜索任务必须使用全新、独立的 workdir**：tile 缓存键只在同一搜索几何
  （同中心、同半径、同 types、同 adcode）下有效，跨参数复用会静默产生覆盖空洞。
- 脚本内置：满页探测（不信任 `count`）、第 8 页仍满 25 条强制四分、
  QPS 退避重试、失败格最多 5 轮重试。结束时失败数必须为 0，否则让用户检查配额后重跑。
- 若手上有"应被包含的更小结果集"（如某区全集 ⊂ 大圆结果集），抓取后做一次
  子集审计，差集必须为 0。
- 提前给用户预期耗时：高密度区域/大半径约 10～20 分钟。

## 第 4 步：整理出最终 CSV

```bash
python3 scripts/amap_poi.py finalize \
  --raw raw_circle.csv \
  --project-lon 119.922427 --project-lat 32.435380 \
  --out final_circle.csv
```

finalize 完成三件事：拆分三级 type、同店清洗（同名同址/同名同本地号码；
400/100 品牌客服号不算重复）、计算每家到项目的 Haversine 大圆距离（米），
并按距离升序输出。

## CSV 表头（严格按此交付）

**raw（18 列，抓取阶段）**：

```
id,name,type,typecode,province,city,district,address,lon,lat,
distance_m,tel,tags,cost,rating,opentime,business_area,website
```

注：`distance_m` 仅周边搜索 API 有值，本技能的网格多边形搜索中为空，
最终距离以 finalize 计算的列为准。

**最终产物（20 列）**：

```
id,name,type_l1,type_l2,type_l3,typecode,province,city,district,address,
lon,lat,tel,tags,cost,rating,opentime,business_area,website,
distance_to_project_m
```

- type 按 `;` 切三级，例如 `餐饮服务;中餐厅;江苏菜` →
  type_l1=餐饮服务, type_l2=中餐厅, type_l3=江苏菜。
- `distance_to_project_m` 是球面大圆直线距离（非沿路路程；沿路距离需另接
  路径规划 API，本技能不提供）。
- CSV 一律 `utf-8-sig` 编码，Excel 双击中文不乱码。

## 交付前自检

1. 行数：与脚本完成日志一致；最终文件抽样核对首/中/末几行。
2. 用项目自身（或项目内的店）验证距离：距离应为几十米级而非数公里，
   若不对，几乎一定是项目坐标取错（回到第 2 步）。
3. 电话列非空比例（城市餐饮通常 50%～70%；无电话是商家未登记，不补造）。

## 参考文件

- [references/pitfalls.md](references/pitfalls.md) — 实战踩坑全记录，动手前必读
- [references/algorithm_and_fields.md](references/algorithm_and_fields.md) —
  四叉树算法、字段映射与高德错误码
- [references/mcp_setup.md](references/mcp_setup.md) — MCP 配置文件路径与配置示例
- `examples/` — 两种模式的真实产物示例（仅展示表头与数据形态）
