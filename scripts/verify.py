#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""交付前校验。

硬门槛（扫码等效，可靠）：
  * 页面尺寸精确落在 trim/bleed（--expect 可断言版式），各页一致
  * 150dpi 逐页逐槽像素反解码 mismatch=0
  * 黑条无色偏（R/G/B 三通道差 ≤24，防四色黑/灰）
  * 残留裁切框/页边框/标记 = 0

参考诊断（audit.py，结构层）：纯 CMYK/叠印/几何/字体/内容流反解码。
  注意：设计稿若在码区自带叠印色块（如 V3 rich-black 底图），内容流启发式会把
  设计色块误判成码条而误报；此时以像素层为准。--strict 可把结构层也升级为硬门槛
  （仅适用于码区干净的设计）。

    python verify.py --input out.pdf [out_转曲.pdf] --model v3 --rule diff [--start 1] [--expect bleed] [--strict]
"""
import argparse
import os
import sys

import pymupdf

import rules
from catalog_util import (DEFAULT_CATALOG, load_catalog, resolve_model,
                          size_state, state_label)

MM = 72 / 25.4
CAST_TOL = 24


def geometry(catalog, model, page_w, page_h, expect="auto"):
    _, meta = resolve_model(catalog, model)
    tpl = catalog["templates"][meta["template"]]
    tw, th = meta["trim"]
    bw, bh = meta["bleed"]
    state, info = size_state(meta, page_w, page_h)
    if state is None:
        raise ValueError("页面 %.2fx%.2fmm 与型号期望 %.0fx%.0f(未出血)/%.0fx%.0f(出血) 都不符，多半型号选错"
                         % (page_w, page_h, tw, th, bw, bh))
    if expect != "auto" and state != expect:
        raise ValueError("页面是%s版 %.2fx%.2fmm，但要求%s版"
                         % (state_label(state), page_w, page_h, state_label(expect)))
    shift = 3.0 if state == "bleed" else 0.0
    bars = [(s["x"] + shift, s["w"], s["bit"]) for s in tpl["slots"]]
    return bars, tpl["bar_h"] + shift, state, meta


def count_residue(pdf, meta, pw, ph):
    tw, th = meta["trim"]
    bw, bh = meta["bleed"]
    d = pymupdf.open(pdf)
    n = 0
    for page in d:
        for dr in page.get_drawings():
            if dr["type"] not in ("s", "fs"):
                continue
            if dr["type"] == "fs" and (dr.get("width") or 0) > 0.6:
                continue
            r = dr["rect"]
            w, h = r.width / MM, r.height / MM
            if ((abs(w - tw) < 1.6 and abs(h - th) < 1.6)
                    or (abs(w - pw) < 1.6 and abs(h - ph) < 1.6)
                    or (abs(w - bw) < 1.6 and abs(h - bh) < 1.6)):
                n += 1
    d.close()
    return n


def decode(pdf, model, catalog, dpi=150, expect="auto"):
    doc = pymupdf.open(pdf)
    p0 = doc[0]
    pw, ph = p0.rect.width / MM, p0.rect.height / MM
    bars, bar_h, state, meta = geometry(catalog, model, pw, ph, expect)
    ppm = dpi / 25.4
    out, casts, odd = [], 0, []
    for i, page in enumerate(doc):
        w, h = page.rect.width / MM, page.rect.height / MM
        if abs(w - pw) > 0.05 or abs(h - ph) > 0.05:
            odd.append(i + 1)
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB)
        row = max(0, min(pix.height - 1, int((h - bar_h / 2) * ppm)))
        base = row * pix.stride
        val = 0
        for x, bw, bit in bars:
            c = max(0, min(pix.width - 1, int((x + bw / 2) * ppm)))
            off = base + c * pix.n
            r, g, b = pix.samples[off], pix.samples[off + 1], pix.samples[off + 2]
            if r + g + b < 200:
                val |= 1 << bit
                if max(r, g, b) - min(r, g, b) > CAST_TOL:
                    casts += 1
        out.append(val)
    doc.close()
    return out, (pw, ph), casts, state, meta, odd


def self_check(pdf, model, rule, start, catalog, expect="auto"):
    """硬门槛自检，返回 (ok, 摘要)。stamp.py 保存后调用。"""
    try:
        vals, size, casts, state, meta, odd = decode(pdf, model, catalog, expect=expect)
    except ValueError as e:
        return False, str(e)
    try:
        exp, _ = rules.codes_with_start(len(vals), rule, start, meta["max_code"])
    except ValueError as e:
        return False, "规则: %s" % e
    bad = [(i + 1, v, exp[i]) for i, v in enumerate(vals) if v != exp[i]]
    residue = count_residue(pdf, meta, size[0], size[1])
    ok = not bad and casts == 0 and residue == 0 and not odd
    msg = ("pages=%d %s版 %.0fx%.0fmm mismatch=%d 色偏=%d 残留=%d 异尺寸页=%d"
           % (len(vals), state_label(state), size[0], size[1], len(bad), casts, residue, len(odd)))
    if bad:
        msg += " 坏页%s" % bad[:8]
    return ok, msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", nargs="+", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--rule", default="diff")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--catalog", default=DEFAULT_CATALOG)
    ap.add_argument("--expect", default="auto", choices=["auto", "bleed", "trim"])
    ap.add_argument("--strict", action="store_true", help="把结构审计也作为硬门槛（码区干净的设计）")
    ap.add_argument("--no-structure", action="store_true", help="不打印结构审计参考")
    a = ap.parse_args()
    catalog = load_catalog(a.catalog)
    key, meta = resolve_model(catalog, a.model)
    print("model=%s(%dbit) template=%s rule=%s start=%d" % (key, meta["bits"], meta["template"], a.rule, a.start))

    ok = True
    for pdf in a.input:
        print("--", os.path.basename(pdf))
        good, msg = self_check(pdf, key, a.rule, a.start, catalog, a.expect)
        print("像素硬门槛:", msg, "OK" if good else "FAIL")
        ok &= good
        if not a.no_structure:
            import audit as audit_mod
            print("结构审计(参考%s):" % ("，硬门槛" if a.strict else ""))
            sok = audit_mod.audit(pdf, key, a.rule, catalog, start=a.start)
            if a.strict:
                ok &= sok
    print("\n结论:", "全部通过，可交付" if ok else "存在失败项，不得交付")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
