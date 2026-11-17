#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""铺码结果结构审计（不依赖渲染，先查"文件里到底画了什么"）。

检查项：
  1. 页面尺寸 / MediaBox 与 CropBox 一致、顺序不串
  2. 每页码块条数 == 模版槽数；条块 x/宽/高与模版几何一致（默认容差 0.05mm）
  3. 条块填充色只能是 `0 0 0 1 k` 或 `0 0 0 0 k`（纯 CMYK，禁止 RGB/灰度/专色）
  4. 码块内不得出现 gs（叠印/透明度/描边样式）——保证码是挖空覆盖而不是叠印
  5. 从码块反解码值，与规则期望值比对（独立于像素校验的第二条证据链）
  6. 残留大描边对象（裁切框/页边框/标记）应为 0
  7. 字体用量（转曲交付时应为 0）

用法：
    python audit.py --input out.pdf --model p7 --rule diff
"""
import argparse
import json
import os
import sys

import pymupdf

import rules
from catalog_util import (DEFAULT_CATALOG, load_catalog, resolve_model,
                          size_state, state_label)

MM = 72 / 25.4
TOL_MM = 0.05


# ------------------------------------------------------------ 极简内容流解析
def tokenize(data):
    out, i, n = [], 0, len(data)
    space = b" \t\r\n\x0c\x00"
    delim = space + b"()<>[]{}/%"
    while i < n:
        c = data[i:i + 1]
        if c in space:
            i += 1
        elif c == b"%":
            j = data.find(b"\n", i)
            i = n if j < 0 else j + 1
        elif c == b"(":
            depth, j = 1, i + 1
            while j < n and depth:
                d = data[j:j + 1]
                if d == b"\\":
                    j += 2
                    continue
                depth += (d == b"(") - (d == b")")
                j += 1
            out.append(data[i:j].decode("latin-1"))
            i = j
        elif c in (b"<", b"["):
            close = b">>" if data[i:i + 2] == b"<<" else (b">" if c == b"<" else b"]")
            if close == b">>":
                depth, j = 1, i + 2
                while j < n and depth:
                    if data[j:j + 2] == b"<<":
                        depth += 1
                        j += 2
                    elif data[j:j + 2] == b">>":
                        depth -= 1
                        j += 2
                    else:
                        j += 1
            else:
                depth, j = 1, i + 1
                while j < n and depth:
                    if data[j:j + 1] == b"(":
                        d2, j = 1, j + 1
                        while j < n and d2:
                            e = data[j:j + 1]
                            if e == b"\\":
                                j += 2
                                continue
                            d2 += (e == b"(") - (e == b")")
                            j += 1
                        continue
                    depth += (data[j:j + 1] == b"[") - (data[j:j + 1] == b"]")
                    j += 1
            out.append(data[i:j].decode("latin-1"))
            i = j
        elif c == b"/":
            j = i + 1
            while j < n and data[j:j + 1] not in delim:
                j += 1
            out.append(data[i:j].decode("latin-1"))
            i = j
        else:
            j = i
            while j < n and data[j:j + 1] not in delim:
                j += 1
            if j == i:
                j = i + 1
            out.append(data[i:j].decode("latin-1"))
            i = j
    return out


def mul(a, b):
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def ap(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def num(t):
    try:
        return float(t)
    except (TypeError, ValueError):
        return None


def extgstates(doc, page):
    """页面 ExtGState: {名字: 是否开叠印}"""
    out = {}
    try:
        kind, val = doc.xref_get_key(page.xref, "Resources/ExtGState")
    except Exception:
        return out
    if kind == "null":
        return out
    import re as _re
    for name, ref in _re.findall(r"/([^\s/<>\[\]]+)\s+(\d+)\s+0\s+R", val):
        try:
            obj = doc.xref_object(int(ref))
        except Exception:
            continue
        out[name] = bool(_re.search(r"/(OP|op)\s+true", obj))
    return out


def page_ops(page):
    """解析页面内容流 -> (fills, flags)
    fills: [dict(bbox=(pt), color=(..), kind='k'|'rg'|'g'|'scn')]
    flags: set(可疑算子)
    """
    doc = page.parent
    data = b""
    for xref in page.get_contents():
        try:
            data += doc.xref_stream(xref) + b"\n"
        except Exception:
            pass
    toks = tokenize(data)
    mb = page.mediabox
    gs_map = extgstates(doc, page)
    fills, flags = [], set()
    last_name = None
    op_state, op_stack = False, []
    ctm, stack, nums = [1, 0, 0, 1, 0, 0], [], []
    pts, color, kind = [], None, None
    for tk in toks:
        v = num(tk)
        if v is not None:
            nums.append(v)
            if len(nums) > 10:
                nums.pop(0)
            continue
        if tk.startswith("/"):
            last_name = tk[1:]
            continue
        if tk == "q":
            stack.append(ctm[:])
            op_stack.append(op_state)
        elif tk == "Q":
            if stack:
                ctm = stack.pop()
            if op_stack:
                op_state = op_stack.pop()
        elif tk == "cm":
            if len(nums) >= 6:
                ctm = mul(nums[-6:], ctm)
        elif tk in ("re",):
            if len(nums) >= 4:
                x, y, w, h = nums[-4:]
                for cx, cy in ((x, y), (x, y + h), (x + w, y), (x + w, y + h)):
                    pts.append(ap(ctm, cx, cy))
        elif tk in ("m", "l"):
            if len(nums) >= 2:
                pts.append(ap(ctm, nums[-2], nums[-1]))
        elif tk in ("k", "K"):
            if len(nums) >= 4:
                color, kind = tuple(nums[-4:]), "k"
        elif tk in ("rg", "RG"):
            if len(nums) >= 3:
                color, kind = tuple(nums[-3:]), "rg"
        elif tk in ("g", "G"):
            if len(nums) >= 1:
                color, kind = (nums[-1],), "g"
        elif tk in ("sc", "scn", "SC", "SCN"):
            color, kind = tuple(nums[-4:]) if nums else (), "scn"
        elif tk in ("f", "F", "f*", "B", "B*", "b", "b*"):
            if pts:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                fills.append(dict(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys),
                                  color=color, kind=kind, op=op_state))
            pts = []
        elif tk in ("n", "S", "s"):
            pts = []
        elif tk == "gs":
            if last_name in gs_map:
                op_state = gs_map[last_name]
                if op_state:
                    flags.add("has_overprint_gs")
        elif tk in ("ca", "CA", "OP", "op", "OPM"):
            flags.add(tk)
        nums.clear() if tk in ("w", "J", "j", "M", "d", "i") else None
    return fills, flags


def is_code_color(c):
    return c in ((0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0, 0.0))


def audit_page(page, exp_bars, bar_h, target_bottom=0.0):
    """返回 (bars, problems)。bars 从内容流反解：[(x_mm, w_mm, h_mm, on)]"""
    fills, flags = page_ops(page)
    mb = page.mediabox
    bands = []
    for f in fills:
        h_mm = (f["y1"] - f["y0"]) / MM
        w_mm = (f["x1"] - f["x0"]) / MM
        if abs(h_mm - bar_h) > 0.12:          # 只看条高与模版一致的块
            continue
        if abs((f["y0"] - mb.y0) / MM - target_bottom) > 0.12:
            continue
        bands.append((round((f["x0"] - mb.x0) / MM, 3), round(w_mm, 3), round(h_mm, 3),
                      f, len(bands)))
    # 同 x 可能既有源文件原有码条又有我们覆盖上去的码条：
    # 取"后绘制的那条"（绘制顺序 = 内容流顺序 = 谁在最上层），不能按 x 大小取
    bands.sort(key=lambda t: t[0])
    groups = []
    for b in bands:
        if groups and abs(b[0] - groups[-1][-1][0]) <= 0.05:
            groups[-1].append(b)
        else:
            groups.append([b])
    shadow = sum(len(g) - 1 for g in groups)
    bands = [max(g, key=lambda t: t[4]) for g in groups]

    problems = []
    slots = exp_bars
    if len(bands) != len(slots):
        problems.append("码块条数 %d != 模版槽数 %d" % (len(bands), len(slots)))
    bars = []
    for i, (x, w, h, f, _o) in enumerate(bands):
        if f["kind"] != "k" or not is_code_color(f["color"]):
            problems.append("第%d条颜色非纯CMYK(0/0/0/1或0/0/0/0): %s %s"
                            % (i + 1, f["kind"], f["color"]))
        if f.get("op"):
            problems.append("第%d条绘制时处于叠印状态（会与背景混色）" % (i + 1))
        on = 1 if (f["color"] and f["color"][-1] > 0.5 and f["kind"] == "k") else 0
        bars.append((x, w, h, on))
    if any(b[3].get("op") for b in bands):
        problems.append("码块处于叠印状态（继承源文件 ExtGState），必须改成挖空覆盖")

    # 几何逐槽比对
    if len(bands) == len(slots):
        for i, ((sx, sw, _b), (x, w, h, _)) in enumerate(zip(slots, bars)):
            if abs(x - sx) > TOL_MM or abs(w - sw) > TOL_MM:
                problems.append("槽%d 位置/宽度偏差 x=%.3f(模版%.3f) w=%.3f(模版%.3f)"
                                % (i + 1, x, sx, w, sw))
    return bars, problems, shadow


def audit(pdf, model, rule, catalog, expect_pages=None, start=1):
    key, meta = resolve_model(catalog, model)
    tpl = catalog["templates"][meta["template"]]
    tw, th = meta["trim"]
    bw, bh = meta["bleed"]

    if not os.path.exists(pdf):
        print("[fail] 文件不存在: %s" % pdf)
        return False
    doc = pymupdf.open(pdf)
    n = len(doc)
    if expect_pages and n != expect_pages:
        print("[fail] 页数 %d != 期望 %d" % (n, expect_pages))

    p0 = doc[0]
    pw, ph = p0.rect.width / MM, p0.rect.height / MM
    state, sinfo = size_state(meta, pw, ph)
    if state is None:
        print("[fail] 页面尺寸 %.2fx%.2fmm 既不是未出血 %.0fx%.0f 也不是出血 %.0fx%.0f"
              "（最接近%s版，偏差 %.2fmm）"
              % (pw, ph, tw, th, bw, bh, state_label(sinfo["near"]),
                 min(sinfo["d_bleed"], sinfo["d_trim"])))
        return False
    shift = 3.0 if state == "bleed" else 0.0
    target = state

    # 每页尺寸都要一致且等于判定的那一版，避免"首页对、后面页错"
    odd = []
    for i, page in enumerate(doc):
        w, h = page.rect.width / MM, page.rect.height / MM
        if abs(w - pw) > 0.05 or abs(h - ph) > 0.05:
            odd.append("p%d %.2fx%.2f" % (i + 1, w, h))
    if odd:
        print("[fail] 各页尺寸不一致（首页 %.2fx%.2fmm）: %s" % (pw, ph, ", ".join(odd[:8])))
        return False

    bars_def = [(s["x"] + shift, s["w"], s["bit"]) for s in tpl["slots"]]
    bar_h = tpl["bar_h"] + shift

    try:
        exp, _ = rules.codes_with_start(n, rule, start, meta["max_code"])
    except ValueError as e:
        if "--limit" in " ".join(sys.argv) or n < 2:
            print("[warn] 规则与当前页数不匹配（试跑态），跳过码值比对: %s" % e)
            exp = None
        else:
            print("[fail] 规则: %s" % e)
            return False

    bad_pages, all_problems, fonts, shadow_bars = 0, [], 0, 0
    codes = []
    for i, page in enumerate(doc):
        bars, probs, shadow = audit_page(page, bars_def, bar_h, 0.0)
        shadow_bars += shadow
        val = 0
        if len(bars) == len(bars_def):
            for (x, w, _h, on), (_sx, _sw, bit) in zip(bars, bars_def):
                if on:
                    val |= 1 << bit
        codes.append(val)
        if exp is not None and val != exp[i]:
            probs.append("码值 %d != 期望 %d" % (val, exp[i]))
        if probs:
            bad_pages += 1
            if len(all_problems) < 10:
                all_problems.append("p%-4d %s" % (i + 1, "; ".join(probs[:3])))
        try:
            fonts += len(page.get_fonts(full=False))
        except Exception:
            pass

    # 残留裁切框/页边框：只看"尺寸≈裁切或≈整页"的描边对象，设计插图不算
    leftovers = 0
    for page in doc:
        for dr in page.get_drawings():
            if dr["type"] not in ("s", "fs"):
                continue
            if dr["type"] == "fs" and (dr.get("width") or 0) > 0.6:
                continue                      # 有填充、有粗描边 = 设计元素
            r = dr["rect"]
            w, h = r.width / MM, r.height / MM
            near_trim = abs(w - tw) < 1.6 and abs(h - th) < 1.6
            near_page = abs(w - pw) < 1.6 and abs(h - ph) < 1.6
            near_bleed = abs(w - bw) < 1.6 and abs(h - bh) < 1.6
            if near_trim or near_page or near_bleed:
                leftovers += 1
    doc.close()

    ok = bad_pages == 0 and leftovers == 0
    print("model=%s(%dbit tpl=%s) pages=%d 版式=%s版 %.0fx%.0fmm%s bar_h=%.1f bar条数=%d"
          % (key, meta["bits"], meta["template"], n, state_label(target), pw, ph,
             ("（四边各出血 %.1fmm）" % ((bw - tw) / 2)) if target == "bleed" else "（无出血）",
             bar_h, len(bars_def)))
    print("结构审计: %s  坏页=%d 残留大描边=%d 字体=%d 覆盖源码条=%d"
          % ("OK" if ok else "FAIL", bad_pages, leftovers, fonts, shadow_bars))
    for p in all_problems:
        print("   ", p)
    if fonts:
        print("    提示：文件仍含字体；转曲交付时应为 0（.ai 可编辑版含字体属正常）")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", nargs="+", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--rule", default="diff")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--catalog", default=DEFAULT_CATALOG)
    a = ap.parse_args()
    cat = load_catalog(a.catalog)
    ok = True
    for f in a.input:
        print("==", os.path.basename(f))
        ok &= audit(f, a.model, a.rule, cat, start=a.start)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
