#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给 PDF 注入印刷输出意图（OutputIntent），补齐 Illustrator 导出会丢的信息。

Illustrator 保存 PDF 时不写 /OutputIntents，印厂做色彩管理时会按"无输出意图"处理。
本脚本把源 PDF 里已有的 ICC 输出意图（一般是 FOGRA39 / Coated FOGRA39）搬到目标 PDF：

    python inject_output_intent.py --target out_转曲.pdf --source src_铺码.pdf
    python inject_output_intent.py --target out.pdf --icc FOGRA39.icc --name FOGRA39

已经存在 OutputIntents 时默认不覆盖（--force 覆盖）。
"""
import argparse
import os
import re
import sys

import pymupdf


def _split_array(txt):
    """把 "/A [(x) <<...>> 12 0 R]" 里的数组内容切成条目（支持嵌套字典）"""
    m = re.search(r"\[(.*)\]", txt, re.S)
    if not m:
        return []
    body, i, n, out = m.group(1), 0, len(m.group(1)), []
    while i < n:
        if body[i].isspace():
            i += 1
            continue
        if body.startswith("<<", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if body.startswith("<<", j):
                    depth += 1
                    j += 2
                elif body.startswith(">>", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            out.append(body[i:j])
            i = j
        elif body.startswith("(", i):
            depth, j = 1, i + 1
            while j < n and depth:
                if body[j] == "\\":
                    j += 2
                    continue
                if body[j] == "(":
                    depth += 1
                elif body[j] == ")":
                    depth -= 1
                j += 1
            out.append(body[i:j])
            i = j
        else:
            j = i
            while j < n and not body[j].isspace():
                j += 1
            out.append(body[i:j])
            i = j
    return out


def get_output_intents(doc):
    """返回 [{ident, subtype, icc_xref}]；兼容内联字典与间接引用两种写法"""
    nb = doc.xref_get_key(doc.pdf_catalog(), "OutputIntents")
    if nb[0] == "null":
        return []
    items = _split_array(nb[1])
    out = []
    for it in items:
        if it.endswith(" R"):
            num = it.split()[0]
            if num.isdigit():
                it = doc.xref_object(int(num))
        ident = re.search(r"/OutputConditionIdentifier\s*\(([^)]*)\)", it)
        sub = re.search(r"/S\s*/(\w+)", it)
        icc = re.search(r"/DestOutputProfile\s+(\d+)\s+0\s+R", it)
        out.append(dict(ident=(ident.group(1) if ident else ""),
                        subtype=(sub.group(1) if sub else ""),
                        icc_xref=(int(icc.group(1)) if icc else None)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="要补 OutputIntent 的 PDF")
    ap.add_argument("--source", help="从哪个 PDF 取现成的 ICC 输出意图")
    ap.add_argument("--icc", help="或直接给 ICC 文件路径")
    ap.add_argument("--name", default="FOGRA39", help="OutputConditionIdentifier")
    ap.add_argument("--force", action="store_true", help="目标已有 OutputIntent 也覆盖")
    ap.add_argument("--out", help="输出路径（默认原地保存）")
    a = ap.parse_args()

    tgt = pymupdf.open(a.target)
    have = get_output_intents(tgt)
    if have and not a.force:
        print("目标已有 OutputIntent: %s（用 --force 覆盖）"
              % ", ".join("%s/%s" % (h["ident"], h["subtype"]) for h in have))
        return 0

    icc_bytes, ident = None, a.name
    if a.source:
        src = pymupdf.open(a.source)
        oi = get_output_intents(src)
        if not oi or not oi[0]["icc_xref"]:
            print("[fail] 源文件 %s 没有可复用的 OutputIntent" % a.source)
            return 1
        icc_bytes = src.xref_stream(oi[0]["icc_xref"])
        if oi[0]["ident"] and oi[0]["ident"] not in ("Custom", ""):
            ident = oi[0]["ident"]
        print("取到源输出意图: %s (ICC %d bytes)" % (ident, len(icc_bytes)))
        src.close()
    elif a.icc:
        icc_bytes = open(a.icc, "rb").read()
    else:
        print("[fail] 需要 --source 或 --icc 之一")
        return 1

    # 建 ICCBased 色彩空间流对象
    icc_xref = tgt.get_new_xref()
    tgt.update_object(icc_xref, "<< /N 4 /Alternate /DeviceCMYK >>")
    tgt.update_stream(icc_xref, icc_bytes, new=True, compress=True)

    oi_xref = tgt.get_new_xref()
    tgt.update_object(oi_xref,
                      "<< /Type /OutputIntent /S /GTS_PDFX "
                      "/OutputConditionIdentifier (%s) /Info (%s) "
                      "/RegistryName (http://www.color.org) "
                      "/DestOutputProfile %d 0 R >>" % (ident, ident, icc_xref))
    tgt.xref_set_key(tgt.pdf_catalog(), "OutputIntents", "[%d 0 R]" % oi_xref)

    out = a.out or a.target
    tmp = out + ".tmp"
    tgt.save(tmp, garbage=3, deflate=True)
    tgt.close()
    os.replace(tmp, out)

    chk = pymupdf.open(out)
    ok = bool(get_output_intents(chk))
    chk.close()
    print("%s: %s" % ("已注入" if ok else "注入失败", out))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
