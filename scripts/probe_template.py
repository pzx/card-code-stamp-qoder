#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
从 base 模版 PDF 反解码带几何与位序，生成 catalog.json（templates + models）。

用法:
    python probe_template.py --base <模版目录> --out <catalog.json>

要点:
 1. 只分析“未出血”模版；出血版 = 未出血几何 +3mm（x 平移 3mm、条高 +3mm），
    已用 V3 / p7 / s7 / 小卡 四组模版双向核对过。
 2. 码带贴页面底边、条块等高同底，按 (高, 底边y) 取最大簇，排除设计元素。
 3. 位序反解：逐槽构造全页黑白向量，与候选码序列各位模式比对，
    取“错配最少且构成双射”的分配（模版偶有设计元素压码，允许少量错配）。
"""
import argparse, glob, json, os
from collections import Counter
import pymupdf

MM = 72 / 25.4

# 型号 -> 模版（一个模版可服务多个型号；bits 决定模版槽数）
# 注意：裸型号（p6/p6b/p7）默认指"大卡"，小卡一律带 -小卡 后缀。
MODEL_GROUPS = [
    dict(key="60x90_10slots", size=(60.0, 90.0), bits=10, max_code=1023,
         models=["v2", "v7c", "p5", "p6", "p6b", "p7"], pending=[],
         note="大卡系列：v2/V7c/P5/P6/P6B/P7 共用一个模版（60x90，10 槽）"),
    dict(key="35x80_6slots", size=(35.0, 80.0), bits=6, max_code=63,
         models=["p5-小卡", "p6-小卡"], pending=[], note="小卡 6 槽：p5-小卡 / p6-小卡 共用（35x80）"),
    dict(key="35x80_8slots", size=(35.0, 80.0), bits=8, max_code=255,
         models=["p6b-小卡", "p7-小卡"], pending=[], note="p6b/p7 小卡（35x80，8 槽）"),
    dict(key="105x160_10slots", size=(105.0, 160.0), bits=10, max_code=1023,
         models=["v3"], pending=[], note="v3 专用，位序特殊"),
    dict(key="75x105_10slots", size=(75.0, 105.0), bits=10, max_code=1023,
         models=["s7", "v11"], pending=[], note="s7/v11 共用（75x105，10 槽）"),
]
# 兼容旧引用
SIZE_MODELS = MODEL_GROUPS


def is_white(f):
    if not f:
        return False
    return min(f) > 0.97 if len(f) == 3 else max(f) < 0.03


def is_black(f):
    if not f:
        return False
    return max(f) < 0.3 if len(f) == 3 else (f[3] > 0.5 and max(f[:3]) < 0.1)


def band_rows(page):
    cand = []
    for dr in page.get_drawings():
        if dr['type'] not in ('f', 'fs') or not dr['fill']:
            continue
        r = dr['rect']
        w, h = r.width / MM, r.height / MM
        if not (3.0 < w < 18 and 2.5 < h < 14):
            continue
        if not (is_white(dr['fill']) or is_black(dr['fill'])):
            continue
        cand.append((round(h, 1), round(r.y1 / MM, 1), round(r.x0 / MM, 3),
                     round(w, 2), is_black(dr['fill'])))
    if not cand:
        return [], None
    (bh, by), _ = Counter((h, y) for h, y, *_ in cand).most_common(1)[0]
    rows = [(x, w, k) for h, y, x, w, k in cand if h == bh and y == by]
    best = {}
    for x, w, k in rows:
        # 同 x 可能同时存在黑条与被白色补块覆盖的同位白块 —— 黑必须胜出
        if x not in best or (k and not best[x][1]) or (k == best[x][1] and w > best[x][0]):
            best[x] = (w, k)
    return sorted((x, w, k) for x, (w, k) in best.items()), bh


def cluster(xs, tol=0.7):
    out = []
    for x in sorted(xs):
        if out and x - out[-1][-1] <= tol:
            out[-1].append(x)
        else:
            out.append([x])
    return [sum(g) / len(g) for g in out]


def sequences(n):
    return {
        "front_back_diff_1based": [(i + 2 if i % 2 == 0 else i) for i in range(n)],
        "front_back_diff_0based": [(i + 1 if i % 2 == 0 else i - 1) for i in range(n)],
        "sequential_1based": [i + 1 for i in range(n)],
        "sequential_0based": [i for i in range(n)],
    }


def solve_mapping(mat, nbits, max_err_ratio=0.02):
    n, nslots = len(mat), len(mat[0])
    best = None
    for name, codes in sequences(n).items():
        if max(codes) >= (1 << nbits):
            continue
        bitpat = [[(c >> b) & 1 for c in codes] for b in range(nbits)]
        # 代价矩阵：槽 j 对应 bit b 的错配数
        cost = [[sum(1 for i in range(n) if mat[i][j] != bitpat[b][i]) for b in range(nbits)]
                for j in range(nslots)]
        # 贪心 + 校验：优先全局最小错配的双射
        pairs = sorted(((cost[j][b], j, b) for j in range(nslots) for b in range(nbits)))
        mapping, usedJ, usedB, total = {}, set(), set(), 0
        for c, j, b in pairs:
            if j in usedJ or b in usedB:
                continue
            mapping[j] = b
            usedJ.add(j); usedB.add(b); total += c
            if len(mapping) == nslots:
                break
        if len(mapping) != nslots:
            continue
        vals = [sum(1 << mapping[j] for j in range(nslots) if mat[i][j]) for i in range(n)]
        exact = sum(1 for i in range(n) if vals[i] == codes[i])
        if exact >= n * (1 - max_err_ratio):
            cand = (total, -exact, name, dict(mapping))
            if best is None or cand[:2] < best[:2]:
                best = cand
    return (best[2], best[3]) if best else (None, None)


def probe(path):
    d = pymupdf.open(path)
    W, H = round(d[0].rect.width / MM, 2), round(d[0].rect.height / MM, 2)
    mats, xs, widths, heights = [], [], {}, []
    for p in d:
        rows, bh = band_rows(p)
        if not rows:
            mats.append(None)
            continue
        heights.append(bh)
        for x, w, k in rows:
            xs.append(x)
            widths.setdefault(round(x, 1), []).append(w)
        mats.append(rows)
    if not xs:
        return None
    slot_x = [round(x, 2) for x in cluster(xs)]
    slot_w = []
    for sx in slot_x:
        vals = [w for x, ws in widths.items() if abs(x - sx) <= 0.7 for w in ws]
        slot_w.append(round(sum(vals) / len(vals), 2) if vals else 0.0)
    mat = []
    for rows in mats:
        row = [0] * len(slot_x)
        for x, w, k in (rows or []):
            j = min(range(len(slot_x)), key=lambda t: abs(slot_x[t] - x))
            if k:                      # 黑优先：设计元素的白块不得覆盖码条
                row[j] = 1
        mat.append(row)
    scheme, mapping = solve_mapping(mat, len(slot_x))
    return dict(page_w=W, page_h=H, pages=len(d),
                bar_h=round(sum(heights) / len(heights), 2),
                slots=[dict(x=x, w=w, bit=(mapping[j] if mapping else None))
                       for j, (x, w) in enumerate(zip(slot_x, slot_w))],
                scheme=scheme,
                decode_rate=(None if not scheme else
                             round(sum(1 for i in range(len(mat))
                                       if sum(1 << mapping[j] for j in range(len(slot_x)) if mat[i][j])
                                       == sequences(len(mat))[scheme][i]) / len(mat), 4)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", default="catalog.json")
    a = ap.parse_args()

    templates, models = {}, {}
    for f in sorted(glob.glob(os.path.join(a.base, "*.pdf"))):
        name = os.path.basename(f)
        info = probe(f)
        if not info:
            print("[skip] no code band:", name)
            continue
        w, h = info["page_w"], info["page_h"]
        # 出血版：尺寸 = 某个 trim + 6，则跳过（几何可由 trim 推导）
        is_bled = any(abs(w - (tw + 6)) < 0.6 and abs(h - (th + 6)) < 0.6
                      for g in MODEL_GROUPS for tw, th in [g["size"]])
        if is_bled:
            print("[skip] bleed template (derive from trim):", name)
            continue
        key = "%gx%g_%dslots" % (w, h, len(info["slots"]))
        if key in templates:
            # 同尺寸同槽数只能有一个模版；否则按尺寸反查型号会静默取错
            print("[warn] duplicate template key %s: keep %s, skip %s"
                  % (key, templates[key]["file"], name))
            continue
        info.update(key=key, file=name)
        templates[key] = info
        print("[ok] %-56s %gx%g slots=%-2d bar_h=%.1f scheme=%s decode=%s"
              % (name[:56], w, h, len(info["slots"]), info["bar_h"],
                 info["scheme"], info["decode_rate"]))

    for g in MODEL_GROUPS:
        tw, th = g["size"]
        key = g["key"]
        if key not in templates:
            print("[miss] template %s not found in base/, 型号 %s 未写入"
                  % (key, ", ".join(g["models"])))
            continue
        tpl = templates[key]
        tpl["models"] = list(g["models"])
        tpl["pending_models"] = list(g.get("pending") or [])
        tpl["note"] = g.get("note", "")
        for m in g["models"]:
            models[m] = dict(trim=[tw, th], bleed=[tw + 6, th + 6], bits=g["bits"],
                             max_code=g["max_code"], template=key, template_confirmed=True)
        for m in g.get("pending") or []:
            models[m] = dict(trim=[tw, th], bleed=[tw + 6, th + 6], bits=g["bits"],
                             max_code=g["max_code"], template=key, template_confirmed=False)

    catalog = dict(schema=2, templates=templates, models=models,
                   model_groups={g["key"]: list(g["models"]) for g in MODEL_GROUPS},
                   pending_models={g["key"]: list(g["pending"]) for g in MODEL_GROUPS if g.get("pending")})
    json.dump(catalog, open(a.out, "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=1)
    print("\n模版 -> 型号:")
    for g in MODEL_GROUPS:
        if g["key"] in templates:
            print("  %-16s %s%s" % (g["key"], ", ".join(g["models"]),
                                    ("  [待确认: %s]" % ", ".join(g["pending"])) if g.get("pending") else ""))
    print("written:", a.out)


if __name__ == "__main__":
    main()
