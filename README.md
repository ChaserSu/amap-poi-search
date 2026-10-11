# amap-poi-search

基于高德地图 Web 服务 API 的 POI 全量抓取技能（Skill）。解决高德搜索接口**单次请求最多返回 200 条**的限制，支持两种搜索模式，输出含三级业态分类与到指定项目直线距离的 CSV。

## 能力

- **圆形模式（circle）**：以指定项目为中心，抓取指定半径（如 50km）内的所有目标业态 POI
- **行政区模式（district）**：抓取指定项目所在行政区（如市辖区、县级市）边界内的所有目标业态 POI
- 自动通过四叉树递归切分网格，保证每格结果不超 200 条，按 POI ID 全局去重
- 返回高德 `extensions=all` 的完整字段：电话、特色菜标签、人均、评分、营业时间、商圈、官网等
- 后处理：`type` 拆分为三级业态列，计算每个 POI 到指定项目的 Haversine 直线距离

## 安装到 TRAE

一句话命令安装（全局，TRAE 中国版）：

```bash
npx skills add ChaserSu/amap-poi-search --agent trae-cn -g -y
```

## 前置条件

1. **高德 API Key**（Web 服务类型）：默认使用高德官方域名 `https://restapi.amap.com`，在[高德开放平台控制台](https://console.amap.com/dev/key/app)创建「Web服务」类型的 Key。免费 Key 即可使用，受每日调用总量与 QPS 限制。
   若你有自建或第三方的高德兼容中转站，可设置环境变量 `AMAP_BASE` 指向它（Key 必须与该域名配套）。
2. （可选）安装高德 MCP。本 Skill 也可以不依赖 MCP，直接用 `curl`/Python 请求 REST API。

## 快速开始

核心脚本零第三方依赖，仅需 Python 3 标准库：

```bash
export AMAP_KEY="你的高德Web服务Key"
# 默认走高德官方域名；仅当你有自己的中转站时才设置：
# export AMAP_BASE="https://你的中转域名"

# 1. 定位项目（必须用 POI 文本搜索，不要用地理编码，见 pitfalls #1）
python3 scripts/amap_poi.py locate --project "凤城悦天地" --city "泰州"

# 2a. 圆形模式：项目周围 50km 的所有餐饮（types=050000）
python3 scripts/amap_poi.py circle --project "凤城悦天地" --city "泰州" \
    --radius-km 50 --types 050000 --workdir ./work_circle

# 2b. 行政区模式：项目所在行政区内的所有餐饮
python3 scripts/amap_poi.py district --project "凤城悦天地" --city "泰州" \
    --types 050000 --workdir ./work_district

# 3. 后处理：三级业态拆分 + 到项目距离，输出最终 CSV
python3 scripts/amap_poi.py finalize --workdir ./work_district \
    --target-lon 119.922427 --target-lat 32.435380 \
    --out result.csv
```

## 输出 CSV 表头

```
id,name,type,typecode,province,city,district,address,lon,lat,distance_m,
tel,tags,cost,rating,opentime,business_area,website,
type_l1,type_l2,type_l3
```

- 前 18 列为高德原始字段（`distance_m` 除外）
- `type_l1/l2/l3`：将高德 `type`（如「餐饮服务;中餐厅;四川菜」）拆分得到的三级业态
- `distance_m`：到指定项目的球面直线距离（米），最终 CSV 按该列升序

## 费用与配额提醒（重要）

- 高德按 **QPS 与日调用量**计费/限额，脚本内置重试与节流，但**大半径 + 全业态会产生大量调用**（50km 全量餐饮可达数百次请求），跑之前请先在[配额管理](https://console.amap.com/dev/flow/manage)确认余额与额度
- 每个网格结果都缓存在 workdir，中断后重跑会跳过已完成格子；**切换搜索参数（中心、半径、types）时务必使用新的 workdir**，旧缓存会被错误复用导致覆盖空洞
- 单请求 200 条上限无法通过付费或提升 Key 等级解除，只能靠网格拆分

## 目录结构

```
├── SKILL.md                          # Agent 使用的主流程指令
├── scripts/amap_poi.py               # 核心脚本（locate/circle/district/finalize）
├── references/
│   ├── pitfalls.md                   # 14 条实战踩坑记录（强烈建议先读）
│   ├── algorithm_and_fields.md       # 四叉树算法、字段映射、infocode 对照表
│   └── mcp_setup.md                  # MCP 配置方式
└── examples/
    ├── district_hailing_food_example.csv   # 示例：泰州海陵区全量餐饮（5357 家，无空洞）
    └── circle_50km_food_example.csv        # 示例：50km 圆形抓取（旧中心缓存，存在已知覆盖空洞）
```

## 无遗漏的保证与边界

- **几何层面**：四叉树叶子矩形是根包围盒的不交并；只剪枝「最近点仍在目标区域外」的格子，数学上不会产生缝隙或盲区
- **数据层面**：不信任 `count` 字段，采用「满页探测」——第 8 页仍返回满 25 条则强制继续四分；结束后失败格子清单必须为空
- **边界**：以上只保证「高德 POI 数据集内不遗漏」，高德未收录的商家无法获取
