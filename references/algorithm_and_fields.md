# 算法、字段映射与错误码参考

## 一、四叉树网格算法

### 为什么需要网格

同参数翻页最多 200 条。把搜索范围切成足够小的矩形，使每格结果 ≤200，
逐格拉完再按 POI ID 去重，即可还原全量。

### 为什么用"矩形四分"而不是"多个圆叠加"

- 四叉树叶子矩形的并集 = 根矩形，且两两内部不交（只是用中线切十字）。
  集合等式在任意分裂深度下恒成立：**无重叠、无盲区**。
- 多个圆叠加必然存在月牙形盲区与大量重叠区域。

### 节点的两种命运（显式栈实现，非递归）

1. 成为叶子：拉取并缓存，结果保留；
2. 被四分：用四个子矩形完全替代自身。

单格拉取规则（满页探测，不看 count）：

```
逐页请求，每页 25 条，最多 8 页（200）：
  某页返回 < 25 条   → 末页，拉干净 → 存 leaf 缓存
  第 8 页仍满 25 条   → 疑似截断   → 写 split 标记 → 四分
```

### circle 模式的剪枝

对每个矩形计算"圆心 clamp 到矩形的最近点"：
- 最近点到圆心距离 > 半径 → 矩形整体在圆外，丢弃
  （凸性保证：最近点在圆外则矩形所有点都在圆外）；
- 否则保留并整格拉取，事后用 Haversine 精算，只保留距离 ≤ 半径的行。

根矩形 = 圆的外接方形（经纬度方向尺寸按 `R/111000`、
`R/(111320·cos(lat))` 换算，外加 0.002° 缓冲）。

### district 模式的剪枝与过滤

- 边界由行政区 API 获取（`/v3/config/district`，keywords=adcode，
  extensions=all → polyline；多环用 `|` 分隔，对应飞地）；
- 矩形是否与区划多边形相交，三条任一成立即保留：
  矩形关键点在多边形内 / 多边形顶点在矩形内 / 两边相交；
- 最终过滤：POI 坐标点在任一闭合环内（射线法），
有条件时同时比对 `adcode` 做交叉验证。

### 断点续传缓存（workdir）

| 文件 | 含义 |
|---|---|
| `<d>_<ix>_<iy>_leaf.json` | 叶子格拉取结果（raw 字典列表） |
| `<d>_<ix>_<iy>_split.json` | 该节点曾被四分（重跑时不再发请求） |
| `district_<adcode>.json` | 区划边界本地缓存 |

重跑安全：命中缓存零请求；失败格重试最多 5 轮；结束时失败清单必须为空。

## 二、API 字段 → raw CSV 映射

| raw 列 | 高德字段 | 备注 |
|---|---|---|
| id | `id` | |
| name | `name` | |
| type | `type` | 形如 餐饮服务;中餐厅;江苏菜，finalize 时拆三级 |
| typecode | `typecode` | |
| province | `pname` | |
| city | `cityname` | |
| district | `adname` | 区县名（注意不是 adcode） |
| address | `address` | |
| lon, lat | `location` 拆分 | 经度在前 |
| distance_m | `distance` | 仅 around 接口有值；polygon 为空 |
| tel | `tel` | 多个用 `;` 分隔；仅 extensions=all |
| tags | `tag` | 餐饮为特色菜，逗号分隔 |
| cost | `biz_ext.cost` | 人均消费 |
| rating | `biz_ext.rating` | |
| opentime | `biz_ext.opentime2` | 规范营业时间 |
| business_area | `business_area` | 商圈名 |
| website | `website` | |

extensions=all 另外还可取但当前未保存：postcode、email、entr_location、
photos（多图）、alias、gridcode、navi_poiid、indoor_map/indoor_data 等，
需要时在 `poi_to_raw()` 中扩展即可。

## 三、距离口径

`distance_to_project_m` = Haversine 球面大圆距离（地球视为半径 6371km 的球），
是"地图上的直线"，**不是沿路路程**。沿路距离需逐点调用
`/v3/direction/{driving,walking,transit,bicycling}`，本技能不做。

## 四、高德常用 infocode

| code | 含义 | 处置 |
|---|---|---|
| 10000 | 成功 | |
| 10001 | Key 不正确/过期 | 引导检查 Key |
| 10003 | 访问超出日配额 | 控制台查看配额，次日或申请提升 |
| 10004 | 单位时间访问过高（QPS） | 退避重试/降低频率 |
| 10009 | Key 平台类型不匹配 | 换「Web服务」类型 Key；签名无效 |
| 10019 | 存在非法/不支持的参数 | 对照文档核参数 |
| 10020 | 缺少必填参数 | |
| 10021 | 不支持的协议（http/https） | 用 https |
| 10026 | 服务维护中 | 稍后重试 |
| 10044 | 账号日调用量超限 | 同 10003 |
| 20000+ | 各业务参数错误 | 依 info 提示排查 |

限频/未知错误：指数退避 2→4→…→20s，最多 8 次；
`10001/10003/10004/10009` 立即返回，不做无意义重试。

## 五、高德接口清单（本技能用到的）

| 接口 | 用途 |
|---|---|
| GET `/v3/place/text` | locate：关键字 POI 搜索定位项目 |
| GET `/v3/place/polygon` | 核心：小矩形网格抓取（extensions=all） |
| GET `/v3/config/district` | district：获取区划边界与名称 |
