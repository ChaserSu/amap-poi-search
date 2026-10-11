#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
amap_poi.py — 高德 POI 全量抓取与整理（纯标准库，无第三方依赖）

四个子命令：
  locate    用关键字搜索精确定位"项目"的真实坐标（避免地理编码降级到行政区中心）
  circle    以项目为圆心、指定半径(米)，四叉树全量抓取指定业态
  district  指定行政区(adcode)，用区划边界做四叉树全量抓取指定业态
  finalize  读取 raw CSV：拆分 3 级 type + 计算到项目的 Haversine 直线距离，出最终 CSV

Key 获取优先级：--key 参数 > 环境变量 AMAP_KEY
注意：Key 必须是高德「Web服务」类型，否则报 USERKEY_PLAT_NOMATCH (10009)

示例：
  python3 amap_poi.py locate --project "凤城悦天地" --city 泰州
  python3 amap_poi.py circle   --project-lon 119.922427 --project-lat 32.435380 \
                               --radius 50000 --types 050000 \
                               --workdir ./tiles_circle --raw-out raw_circle.csv
  python3 amap_poi.py district --district 321202 --types 050000 \
                               --workdir ./tiles_district --raw-out raw_district.csv
  python3 amap_poi.py finalize --raw raw_circle.csv \
                               --project-lon 119.922427 --project-lat 32.435380 \
                               --out final_circle.csv
"""
import argparse, csv, json, math, os, sys, time, urllib.request, urllib.parse

# ---------------------------------------------------------------- 常量
OFFSET = 25            # 每页条数（超过 25 可能报错）
MAX_PAGES = 8          # 同参数翻页硬上限 200 条
MAX_DEPTH = 12
EARTH_R = 6371000.0
# API 域名：优先官方 https://restapi.amap.com；用户显式提供中转站时用环境变量 AMAP_BASE 覆盖
BASE_URL = os.environ.get("AMAP_BASE", "https://restapi.amap.com").rstrip("/")

RAW_FIELDS = ["id", "name", "type", "typecode", "province", "city", "district",
              "address", "lon", "lat", "distance_m", "tel", "tags", "cost",
              "rating", "opentime", "business_area", "website"]

FINAL_FIELDS = ["id", "name", "type_l1", "type_l2", "type_l3", "typecode",
                "province", "city", "district", "address", "lon", "lat",
                "tel", "tags", "cost", "rating", "opentime", "business_area",
                "website", "distance_to_project_m"]


# ---------------------------------------------------------------- 通用
# 全局调用统计（实际发出的 HTTP 请求次数，含重试；缓存命中不计）
STATS = {"http_calls": 0, "ok": 0, "retry": 0, "t0": time.time()}


def die(msg, code=1):
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(code)


def get_key(args):
    key = getattr(args, "key", None) or os.environ.get("AMAP_KEY")
    if not key:
        die("未找到高德 Key。请用 --key 传入，或设置环境变量 AMAP_KEY。\n"
            "Key 申请：https://console.amap.com → 应用管理 → 创建应用 → 添加 Key（服务平台选「Web服务」）")
    return key


def http_json(url, max_tries=8, tag=""):
    """带指数退避的 GET；infocode!=10000 也算可重试错误。
    ② 每条请求实时打印日志：序号/标签/结果/累计耗时。"""
    for attempt in range(max_tries):
        STATS["http_calls"] += 1
        n = STATS["http_calls"]
        print(f"  [请求#{n:>4}] {tag or 'API'} 第{attempt+1}次 ...", flush=True)
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                d = json.loads(r.read().decode())
            if str(d.get("infocode")) == "10000":
                STATS["ok"] += 1
                npois = len(d.get("pois") or [])
                extra = f" pois={npois}" if "pois" in d else ""
                print(f"  [#{n:>4} OK] {tag} 用时{time.time()-STATS['t0']:.0f}s "
                      f"累计请求{n}{extra}", flush=True)
                return d
            info, code = d.get("info"), d.get("infocode")
            # 平台类型不匹配/Key 无效/额度耗尽类错误重试无意义
            if str(code) in ("10009", "10001", "10003", "10004",
                             "10044", "10020", "10021", "10029", "10019"):
                print(f"  [#{n} 致命] {info} ({code}) —— 不再重试", file=sys.stderr)
                return d
            STATS["retry"] += 1
            wait = min(2 + attempt * 2, 20)
            print(f"  [#{n} 重试] {info} ({code})，{wait}s 后重试", file=sys.stderr)
            time.sleep(wait)
        except Exception as e:
            STATS["retry"] += 1
            wait = min(2 + attempt * 2, 20)
            print(f"  [#{n} 网络异常] {e}，{wait}s 后重试", file=sys.stderr)
            time.sleep(wait)
    return None


def sval(v):
    """高德字段空值可能是 []，统一转字符串。"""
    return v if isinstance(v, str) else ""


def haversine(lon1, lat1, lon2, lat2):
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dlat = r2 - r1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- 几何
def point_in_rings(x, y, rings):
    """射线法；多环（飞地）任一命中即为内部。"""
    for ring in rings:
        inside = False
        jx, jy = ring[-1]
        for i in range(len(ring)):
            xi, yi = ring[i]
            if (yi > y) != (jy > y) and x < (jx - xi) * (y - yi) / (jy - yi) + xi:
                inside = not inside
            jx, jy = xi, yi
        if inside:
            return True
    return False


def _orient(ax, ay, bx, by, cx, cy):
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def seg_intersect(p1, p2, q1, q2):
    o1 = _orient(*p1, *p2, *q1)
    o2 = _orient(*p1, *p2, *q2)
    o3 = _orient(*q1, *q2, *p1)
    o4 = _orient(*q1, *q2, *p2)
    if o1 == 0 and o2 == 0:  # 共线：投影重叠
        return (min(p1[0], p2[0]) <= max(q1[0], q2[0])
                and min(q1[0], q2[0]) <= max(p1[0], p2[0])
                and min(p1[1], p2[1]) <= max(q1[1], q2[1])
                and min(q1[1], q2[1]) <= max(p1[1], p2[1]))
    return ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0))


def rect_corners(b):
    xmin, ymin, xmax, ymax = b
    c = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
    return c, list(zip(c, c[1:] + c[:1]))


def rect_intersects_circle(b, lon0, lat0, radius):
    """矩形上距圆心最近的点在圆内 → 矩形与圆相交。"""
    xmin, ymin, xmax, ymax = b
    cx = min(max(lon0, xmin), xmax)
    cy = min(max(lat0, ymin), ymax)
    return haversine(lon0, lat0, cx, cy) <= radius


def rect_intersects_polygon(b, rings):
    xmin, ymin, xmax, ymax = b
    corners, rect_edges = rect_corners(b)
    # 1) 矩形四角或中心在多边形内
    for px, py in corners + [((xmin + xmax) / 2, (ymin + ymax) / 2)]:
        if point_in_rings(px, py, rings):
            return True
    for ring in rings:
        # 2) 多边形顶点在矩形内
        for px, py in ring:
            if xmin <= px <= xmax and ymin <= py <= ymax:
                return True
        # 3) 边相交
        j = len(ring) - 1
        for i in range(len(ring)):
            e1, e2 = ring[j], ring[i]
            j = i
            for re1, re2 in rect_edges:
                if seg_intersect(e1, e2, re1, re2):
                    return True
    return False


# ---------------------------------------------------------------- 高德 API
def api_text_search(key, keywords, city=None, limit=10):
    params = {"keywords": keywords, "types": "", "city": city or "",
              "citylimit": "true" if city else "false", "offset": limit,
              "page": 1, "extensions": "all", "key": key}
    url = BASE_URL + "/v3/place/text?" + urllib.parse.urlencode(params)
    return http_json(url, tag=f"text:{keywords}")


def api_polygon_search(key, rect, page, types="", tag=""):
    xmin, ymin, xmax, ymax = rect
    poly = f"{xmin:.6f},{ymax:.6f}|{xmax:.6f},{ymin:.6f}"  # 左上|右下
    params = {"polygon": poly, "types": types or "",
              "offset": OFFSET, "page": page,
              "extensions": "all", "key": key}
    url = BASE_URL + "/v3/place/polygon?" + urllib.parse.urlencode(params)
    return http_json(url, tag=tag or f"polygon p{page}")


def api_district(key, adcode):
    params = {"keywords": adcode, "sub_district": 0,
              "extensions": "all", "key": key}
    url = BASE_URL + "/v3/config/district?" + urllib.parse.urlencode(params)
    return http_json(url, tag=f"district:{adcode}")


def poi_to_raw(p):
    be = p.get("biz_ext") or {}
    loc = sval(p.get("location"))
    lon, lat = "", ""
    if loc:
        parts = loc.split(",")
        lon, lat = parts[0], parts[1]
    return {
        "id": sval(p.get("id")),
        "name": sval(p.get("name")),
        "type": sval(p.get("type")),
        "typecode": sval(p.get("typecode")),
        "province": sval(p.get("pname")),
        "city": sval(p.get("cityname")),
        "district": sval(p.get("adname")),
        "address": sval(p.get("address")),
        "lon": lon, "lat": lat,
        "distance_m": sval(p.get("distance")),
        "tel": sval(p.get("tel")),
        "tags": sval(p.get("tag")),
        "cost": sval(be.get("cost")),
        "rating": sval(be.get("rating")),
        "opentime": sval(be.get("opentime2")),
        "business_area": sval(p.get("business_area")),
        "website": sval(p.get("website")),
    }


# ---------------------------------------------------------------- 四叉树
def split_rect(b):
    xmin, ymin, xmax, ymax = b
    mx, my = (xmin + xmax) / 2, (ymin + ymax) / 2
    return [
        (xmin, my, mx, ymax, 0, 0),
        (mx, my, xmax, ymax, 1, 0),
        (xmin, ymin, mx, my, 0, 1),
        (mx, ymin, xmax, my, 1, 1),
    ]


def tile_files(workdir, depth, ix, iy):
    base = os.path.join(workdir, f"{depth}_{ix}_{iy}")
    return base + "_leaf.json", base + "_split.json"


def page_file(workdir, depth, ix, iy, page):
    """③ 每页一个缓存文件，拿到响应立即落盘，进程被杀也不丢。"""
    d = os.path.join(workdir, "pages")
    return os.path.join(d, f"{depth}_{ix}_{iy}_p{page}.json")


def fetch_page(key, workdir, depth, ix, iy, rect, page, types=""):
    """取单页：优先读页缓存；未命中才发请求，成功后立即写盘。
    返回 (chunk_raw_list, response_dict)；致命错误/失败返回 (None, d)。"""
    fp = page_file(workdir, depth, ix, iy, page)
    if os.path.exists(fp):
        rec = json.load(open(fp))
        return rec["pois"], {"_cache": True, "count": rec.get("count", "")}
    tag = f"d{depth}({ix},{iy}) p{page}"
    d = api_polygon_search(key, rect, page, types, tag=tag)
    if d is None or str(d.get("infocode")) != "10000":
        return None, d
    chunk = [poi_to_raw(p) for p in (d.get("pois") or [])]
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"pois": chunk, "count": sval(d.get("count"))}, f,
                  ensure_ascii=False)
    os.replace(tmp, fp)          # 原子落盘
    return chunk, d


def drain_tile(key, workdir, depth, ix, iy, rect, types=""):
    """返回 (raw_list, truncated)；None 表示本瓦片未完成（已抓的页都在盘上）。"""
    leaf_fp, split_fp = tile_files(workdir, depth, ix, iy)
    if os.path.exists(split_fp):
        return [], True
    if os.path.exists(leaf_fp):
        return json.load(open(leaf_fp)), False

    raw = []
    truncated = False
    for page in range(1, MAX_PAGES + 1):
        chunk, d = fetch_page(key, workdir, depth, ix, iy, rect, page, types)
        if chunk is None:
            if d is not None and not d.get("_cache"):
                print(f"    [接口致命] {d.get('info')} ({d.get('infocode')})",
                      file=sys.stderr)
            return None
        raw.extend(chunk)
        if len(chunk) < OFFSET:
            break                          # 末页，已拉干净
        if page == MAX_PAGES:
            truncated = True              # 8 页全满 → 不信任 count，强制分裂
            break
        time.sleep(0.35)
    if truncated:
        with open(split_fp, "w") as f:
            f.write("1")
    else:
        with open(leaf_fp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
    return raw, truncated


def _est_calls(count, depth=0):
    """均匀密度假设下的请求数估算：满 200 的瓦片需 8 次翻页并四分。"""
    if depth >= MAX_DEPTH:
        return math.ceil(min(count, 200) / OFFSET)
    if count < 200:
        return max(1, math.ceil(count / OFFSET))
    return 8 + 4 * _est_calls(count / 4, depth + 1)


def estimate_calls(key, workdir, root_rect, types=""):
    """① 开跑前调用量估算：只探根格第1页，满则再探4个子格第1页（≤5次调用，
    探测页全部写入页缓存，正式抓取时直接复用，不多花一分钱）。"""
    os.makedirs(workdir, exist_ok=True)
    print("[估算] 探测根区域第 1 页（探测结果会被正式抓取复用）...")
    chunk, d = fetch_page(key, workdir, 0, 0, 0, root_rect, 1, types)
    if chunk is None:
        print("[估算] 根区域探测失败，跳过估算直接开始（请关注实时日志）")
        return
    root_count = int(sval(d.get("count")) or 0)
    print(f"[估算] 根区域高德 count={root_count}，第1页返回 {len(chunk)} 条")
    if root_count < 200 and len(chunk) < OFFSET:
        est = max(1, math.ceil(root_count / OFFSET))
        print(f"[估算] 无需分裂，预计请求数 ≈ {est} 次（1 个瓦片翻页）")
        return
    # 根格饱和：探 4 个子格第 1 页，拿真实子格 count
    child_est = 0
    child_counts = []
    for (xmin, ymin, xmax, ymax, dx, dy) in split_rect(root_rect):
        crect = (xmin, ymin, xmax, ymax)
        cchunk, cd = fetch_page(key, workdir, 1, dx, dy, crect, 1, types)
        if cchunk is None:
            child_est = None
            break
        cc = int(sval(cd.get("count")) or (200 if len(cchunk) == OFFSET else 0))
        child_counts.append(cc)
        child_est += _est_calls(cc)
    if child_est is None:
        print(f"[估算] 根区域饱和(count={root_count})，子格探测失败，"
              f"粗略上限 ≈ {_est_calls(root_count)} 次（均匀密度模型）")
        return
    est = child_est + (8 - 1)   # 根格自身 8 页（第1页已付费并缓存）
    print(f"[估算] 根区域饱和，4 子格 count={child_counts}")
    print(f"[估算] 预计总请求数 ≈ {est} 次（均匀密度模型，含内部饱和瓦片每片8页的"
          f"固有成本；实际请以 [请求#N] 日志为准）")
    print("[估算] 外接矩形四角/区外部分会被拉取后再过滤，估算偏保守。"
          "若数量不可接受，请 Ctrl+C 后缩小半径/改行政区模式。")


def process_stack(key, workdir, stack, keep_test, types=""):
    """通用四叉树循环。keep_test(rect)->bool 判断格子是否与目标区域相交。
    返回 (leaf_count, failed[])。"""
    failed = []
    leaves = 0
    t0 = time.time()
    while stack:
        depth, ix, iy, rect = stack.pop()
        if not keep_test(rect):
            continue
        raw, truncated = drain_tile(key, workdir, depth, ix, iy, rect, types)
        if raw is None:
            failed.append((depth, ix, iy, rect))
            continue
        if truncated and depth < MAX_DEPTH:
            for (xmin, ymin, xmax, ymax, dx, dy) in split_rect(rect):
                stack.append((depth + 1, ix * 2 + dx, iy * 2 + dy,
                             (xmin, ymin, xmax, ymax)))
            continue
        if truncated:
            print(f"    [警告] d={depth} 格达到 MAX_DEPTH，仅保留 {len(raw)} 条（可能有缺）",
                  file=sys.stderr)
        leaves += 1
        if leaves % 20 == 0:
            print(f"  叶子 {leaves} | 待处理 {len(stack)} | 失败 {len(failed)} "
                  f"| {time.time() - t0:.0f}s")
        time.sleep(0.3)
    return leaves, failed


def retry_failed(key, workdir, failed, keep_test, types=""):
    """失败格重试，最多 5 轮。"""
    for rnd in range(5):
        if not failed:
            return failed
        print(f"  失败格第 {rnd + 1} 轮重试：{len(failed)} 个")
        todo = failed
        failed = []
        stack = []
        for depth, ix, iy, rect in todo:
            raw, truncated = drain_tile(key, workdir, depth, ix, iy, rect, types)
            if raw is None:
                failed.append((depth, ix, iy, rect))
                continue
            if truncated and depth < MAX_DEPTH:
                for (xmin, ymin, xmax, ymax, dx, dy) in split_rect(rect):
                    stack.append((depth + 1, ix * 2 + dx, iy * 2 + dy,
                                 (xmin, ymin, xmax, ymax)))
            time.sleep(0.3)
        _, failed2 = process_stack(key, workdir, stack, keep_test, types)
        failed.extend(failed2)
    return failed


def merge_tiles(workdir):
    by_id = {}
    for fn in os.listdir(workdir):
        if not fn.endswith("_leaf.json"):
            continue
        for r in json.load(open(os.path.join(workdir, fn))):
            if r["id"] and r["id"] not in by_id:
                by_id[r["id"]] = r
    return list(by_id.values())


def write_raw_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=RAW_FIELDS)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- 子命令
def cmd_locate(args):
    key = get_key(args)
    d = api_text_search(key, args.project, city=args.city, limit=args.limit)
    if d is None or str(d.get("infocode")) != "10000":
        die(f"定位搜索失败：{d.get('info') if d else '无响应'}")
    pois = d.get("pois") or []
    if not pois:
        die(f"未搜索到「{args.project}」，请换关键词或城市")
    print(f"共 {len(pois)} 个候选（请核对名称/地址后取坐标用于抓取）：\n")
    for p in pois:
        print(f"id={p.get('id')}  {p.get('name')}\n"
              f"    地址：{sval(p.get('address'))}\n"
              f"    坐标：{sval(p.get('location'))}   类型：{sval(p.get('type'))}")


def cmd_circle(args):
    key = get_key(args)
    if args.radius > 50000:
        print("  [提示] 高德周边接口半径上限 50000m；本算法用多边形网格拼圆，不受该限，但注意配额",
              file=sys.stderr)
    os.makedirs(args.workdir, exist_ok=True)

    # 外接方形（加少量缓冲）
    dlat = args.radius / 111000.0 + 0.002
    dlon = args.radius / (111320.0 * math.cos(math.radians(args.project_lat))) + 0.002
    root = (args.project_lon - dlon, args.project_lat - dlat,
            args.project_lon + dlon, args.project_lat + dlat)

    def keep_test(rect):
        return rect_intersects_circle(rect, args.project_lon,
                                     args.project_lat, args.radius)

    print(f"[圆形模式] 中心=({args.project_lon},{args.project_lat}) "
          f"半径={args.radius}m types={args.types or '(全部)'}")
    estimate_calls(key, args.workdir, root, args.types)   # ① 先估算（探测页可复用）
    stack = [(0, 0, 0, root)]
    _, failed = process_stack(key, args.workdir, stack, keep_test, args.types)
    failed = retry_failed(key, args.workdir, failed, keep_test, args.types)
    if failed:
        die(f"仍有 {len(failed)} 个格子失败，见 workdir；请检查配额后重跑（断点续传）")

    rows = merge_tiles(args.workdir)
    rows = [r for r in rows if r["lat"]
            and haversine(args.project_lon, args.project_lat,
                          float(r["lon"]), float(r["lat"])) <= args.radius]
    rows.sort(key=lambda r: haversine(args.project_lon, args.project_lat,
                                     float(r["lon"]), float(r["lat"])))
    write_raw_csv(rows, args.raw_out)
    print(f"完成：圆内 {len(rows)} 家 → {args.raw_out}")
    print(f"本次实际 HTTP 请求 {STATS['http_calls']} 次"
          f"（成功 {STATS['ok']}，异常重试 {STATS['retry']}；缓存命中不计费）")


def load_boundary(key, adcode, workdir):
    fp = os.path.join(workdir, f"district_{adcode}.json")
    if os.path.exists(fp):
        return json.load(open(fp))
    d = api_district(key, adcode)
    if d is None or str(d.get("infocode")) != "10000":
        die(f"获取区划边界失败：{d.get('info') if d else '无响应'}")
    dist = (d.get("districts") or [None])[0]
    if not dist:
        die(f"未找到 adcode={adcode} 的行政区")
    rings = [[tuple(map(float, pair.split(","))) for pair in ring.split(";")]
             for ring in sval(dist.get("polyline")).split("|")]
    data = {"name": sval(dist.get("name")), "adcode": sval(dist.get("adcode")),
            "rings": rings}
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def cmd_district(args):
    key = get_key(args)
    os.makedirs(args.workdir, exist_ok=True)
    boundary = load_boundary(key, args.district, args.workdir)
    rings = boundary["rings"]

    allx = [p[0] for ring in rings for p in ring]
    ally = [p[1] for ring in rings for p in ring]
    root = (min(allx), min(ally), max(allx), max(ally))

    def keep_test(rect):
        return rect_intersects_polygon(rect, rings)

    print(f"[行政区模式] {boundary['name']}({boundary['adcode']}) "
          f"边界顶点={sum(len(r) for r in rings)} types={args.types or '(全部)'}")
    estimate_calls(key, args.workdir, root, args.types)  # ① 先估算（探测页可复用）
    stack = [(0, 0, 0, root)]
    _, failed = process_stack(key, args.workdir, stack, keep_test, args.types)
    failed = retry_failed(key, args.workdir, failed, keep_test, args.types)
    if failed:
        die(f"仍有 {len(failed)} 个格子失败，见 workdir；请检查配额后重跑（断点续传）")

    rows = merge_tiles(args.workdir)
    # 以边界几何判定境内（实测几何与 adcode 双标准 100% 一致，
    # 若担心边界精度，可在抓取前自行保留 adcode 做交叉验证）
    kept = [r for r in rows if r["lat"]
            and point_in_rings(float(r["lon"]), float(r["lat"]), rings)]
    kept.sort(key=lambda r: (float(r["lat"]), float(r["lon"])))
    write_raw_csv(kept, args.raw_out)
    print(f"完成：{boundary['name']}境内 {len(kept)} 家 → {args.raw_out}")
    print(f"本次实际 HTTP 请求 {STATS['http_calls']} 次"
          f"（成功 {STATS['ok']}，异常重试 {STATS['retry']}；缓存命中不计费）")


def cmd_finalize(args):
    with open(args.raw, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out = []
    # 同店清洗：同名+同地址 或 同名+同本地号码（400/100 品牌客服号不算）
    seen_addr, seen_tel = set(), set()
    drop = 0
    for r in rows:
        akey = (r.get("name", ""), r.get("address", "")) if r.get("address") else None
        tels = [t.strip() for t in (r.get("tel") or "").split(";")
                if t.strip() and not t.strip().startswith(("400", "100"))]
        tkeys = [(r.get("name", ""), t) for t in tels]
        if (akey and akey in seen_addr) or any(k in seen_tel for k in tkeys):
            drop += 1
            continue
        if akey:
            seen_addr.add(akey)
        seen_tel.update(tkeys)

        parts = (r.get("type") or "").split(";")
        l1 = parts[0] if len(parts) > 0 else ""
        l2 = parts[1] if len(parts) > 1 else ""
        l3 = parts[2] if len(parts) > 2 else ""
        dist = ""
        if r.get("lat") and r.get("lon"):
            dist = round(haversine(args.project_lon, args.project_lat,
                                   float(r["lon"]), float(r["lat"])))
        out.append({
            "id": r.get("id", ""), "name": r.get("name", ""),
            "type_l1": l1, "type_l2": l2, "type_l3": l3,
            "typecode": r.get("typecode", ""),
            "province": r.get("province", ""), "city": r.get("city", ""),
            "district": r.get("district", ""), "address": r.get("address", ""),
            "lon": r.get("lon", ""), "lat": r.get("lat", ""),
            "tel": r.get("tel", ""), "tags": r.get("tags", ""),
            "cost": r.get("cost", ""), "rating": r.get("rating", ""),
            "opentime": r.get("opentime", ""),
            "business_area": r.get("business_area", ""),
            "website": r.get("website", ""),
            "distance_to_project_m": dist,
        })
    out.sort(key=lambda r: r["distance_to_project_m"] if r["distance_to_project_m"] != "" else 10**12)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FINAL_FIELDS)
        w.writeheader()
        w.writerows(out)
    if drop:
        print(f"同店清洗：去除 {drop} 条重复")
    print(f"完成：{len(out)} 家 → {args.out}（已按到项目距离升序）")


# ---------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser(description="高德 POI 全量抓取（四叉树网格）")
    ap.add_argument("--key", help="高德 Web服务 Key（也可用环境变量 AMAP_KEY）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("locate", help="精确定位项目坐标")
    p.add_argument("--project", required=True, help="项目名称，如 凤城悦天地")
    p.add_argument("--city", help="城市，如 泰州（强烈建议填写以提高精度）")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_locate)

    p = sub.add_parser("circle", help="圆心+半径全量抓取")
    p.add_argument("--project-lon", type=float, required=True)
    p.add_argument("--project-lat", type=float, required=True)
    p.add_argument("--radius", type=int, default=5000, help="半径，米")
    p.add_argument("--types", default="050000", help="POI 类型码，多个用 | 分隔；050000=全部餐饮")
    p.add_argument("--workdir", required=True, help="网格缓存目录（断点续传）")
    p.add_argument("--raw-out", required=True, help="raw CSV 输出路径")
    p.set_defaults(func=cmd_circle)

    p = sub.add_parser("district", help="行政区边界内全量抓取")
    p.add_argument("--district", required=True, help="行政区 adcode，如 321202=海陵区")
    p.add_argument("--types", default="050000")
    p.add_argument("--workdir", required=True)
    p.add_argument("--raw-out", required=True)
    p.set_defaults(func=cmd_district)

    p = sub.add_parser("finalize", help="拆分 type + 计算到项目距离，出最终 CSV")
    p.add_argument("--raw", required=True, help="raw CSV")
    p.add_argument("--project-lon", type=float, required=True)
    p.add_argument("--project-lat", type=float, required=True)
    p.add_argument("--out", required=True, help="最终 CSV")
    p.set_defaults(func=cmd_finalize)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
