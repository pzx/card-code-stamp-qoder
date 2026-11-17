#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""卡片铺黑白码（Illustrator JSX 静默管线 + v2 印刷安全校验链）。

    python stamp.py --input input/xx.pdf --model v3 --rule diff [--start 1]
        [--bleed auto] [--outdir output] [--name 名] [--limit 6] [--codes-csv 码表.csv]

PDF 输入 -> 输出 PDF；AI 输入 -> 输出 .ai（可编辑）+ 转曲 PDF。全程静默。
对象级修改在 Illustrator JSX 内完成；型号容错/严谨判版/严格码值/结构审计/输出意图
复用 v2 模块（catalog_util / rules / audit / inject_output_intent）。
规格见 references/models.md；Illustrator 约束见 references/illustrator-automation.md。
"""
import argparse
import csv
import io
import os
import subprocess
import sys
import threading
import time

import rules
from catalog_util import (DEFAULT_CATALOG, describe, load_catalog, resolve_model,
                          size_state, state_label)

MM = 72 / 25.4
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_SIZE_DEV = 3.0


# ---------------------------------------------------------------- 输入分析 / 判版
def scan_input(src, trim):
    import pymupdf
    tw, th = trim
    d = pymupdf.open(src)
    pages = len(d)
    seen, frame_pages = {}, 0
    for p in d:
        k = (round(p.rect.width / MM, 3), round(p.rect.height / MM, 3))
        seen[k] = seen.get(k, 0) + 1
        for dr in p.get_drawings():
            if dr["type"] != "s":
                continue
            r = dr["rect"]
            if abs(r.width / MM - tw) < 1.6 and abs(r.height / MM - th) < 1.6:
                frame_pages += 1
                break
    d.close()
    return pages, [(w, h, c) for (w, h), c in seen.items()], frame_pages


def count_frames(src, trim, limit=0):
    import pymupdf
    tw, th = trim
    d = pymupdf.open(src)
    n = 0
    for i, p in enumerate(d):
        if limit and i >= limit:
            break
        for dr in p.get_drawings():
            r = dr["rect"]
            if (dr["type"] == "s" and abs(r.width / MM - tw) < 0.6
                    and abs(r.height / MM - th) < 0.6 and len(dr["items"]) == 8):
                n += 1
    d.close()
    return n


def suggest_models(catalog, w, h):
    best = []
    for key, meta in sorted(catalog["models"].items()):
        d = min(abs(w - meta["trim"][0]) + abs(h - meta["trim"][1]),
                abs(w - meta["bleed"][0]) + abs(h - meta["bleed"][1]))
        best.append((d, key))
    best.sort()
    return ", ".join("%s(偏差%.1fmm)" % (k, d) for d, k in best[:4])


def choose_target(catalog, meta, mode, sizes, frame_pages=0, pages=0):
    notes = []
    if len(sizes) > 1:
        raise SystemExit("输入各页尺寸不一致，无法判定出血：\n  %s\n请先统一页面尺寸"
                         % "\n  ".join("%.2fx%.2fmm x%d页" % s for s in sizes))
    w, h, _ = sizes[0]
    tw, th = meta["trim"]
    bw, bh = meta["bleed"]
    bl = (bw - tw) / 2
    state, info = size_state(meta, w, h)
    if state is None and frame_pages:
        notes.append("输入是带裁切框的大版 %.2fx%.2fmm（%d/%d 页含 %.0fx%.0f 裁切框），按裁切框居中裁切"
                     % (w, h, frame_pages, pages, tw, th))
        target = "bleed" if mode == "auto" else mode
        if target == "trim":
            notes.append("大版裁切为未出血版 %.0fx%.0f" % (tw, th))
        return target, "marked", notes
    if state is None:
        dev = min(info["d_bleed"], info["d_trim"])
        if dev > MAX_SIZE_DEV:
            raise SystemExit("输入 %.2fx%.2fmm 与型号规格（未出血 %.0fx%.0f / 出血 %.0fx%.0f）偏差都超 %.0fmm"
                             "（最近 %.2fmm）且无裁切框，多半型号选错。\n最接近型号: %s"
                             % (w, h, tw, th, bw, bh, MAX_SIZE_DEV, dev, suggest_models(catalog, w, h)))
        nearest = info["near"] if mode == "auto" else mode
        notes.append("输入非标准尺寸，按最接近的%s版处理（偏差 %.2fmm）" % (state_label(nearest), dev))
        return nearest, nearest, notes
    target = state if mode == "auto" else mode
    if target != state:
        if target == "bleed":
            notes.append("源是未出血版，出出血版只能四边各扩 %.1fmm（扩出白边，真出血请回设计稿做够）" % bl)
        else:
            notes.append("源是出血版，按未出血版输出：四边各裁掉 %.1fmm" % bl)
    return target, state, notes


def state_suffix(stem, target):
    label = state_label(target)
    return stem if label in stem else "%s_%s" % (stem, label)


def geometry(catalog, meta, target):
    tpl = catalog["templates"][meta["template"]]
    tw, th = meta["trim"]
    bw, bh = meta["bleed"]
    bleed = target == "bleed"
    page = (bw, bh) if bleed else (tw, th)
    off = 3.0 if bleed else 0.0
    return {
        "NEW_W": page[0] * MM, "NEW_H": page[1] * MM,
        "TRIM_W": tw * MM, "TRIM_H": th * MM,
        "LEFTS": [(s["x"] + off) * MM for s in tpl["slots"]],
        "WIDTHS": [s["w"] * MM for s in tpl["slots"]],
        "BITOF": [s["bit"] for s in tpl["slots"]],
        "TOP_OFF": (tpl["bar_h"] + off) * MM, "BAR_H": (tpl["bar_h"] + off) * MM,
        "NBARS": len(tpl["slots"]), "size": page, "max_code": meta["max_code"],
    }


# ---------------------------------------------------------------- JSX / COM
def build_jsx(name, **subs):
    s = open(os.path.join(HERE, name), encoding="utf-8").read()
    for k, v in subs.items():
        s = s.replace("{{" + k + "}}", str(v).replace("\\", "/"))
    return "".join(ch if ord(ch) < 128 else "\\u%04x" % ord(ch) for ch in s)


def illustrator_pid():
    out = subprocess.check_output(
        ["tasklist", "/FI", "IMAGENAME eq Illustrator.exe", "/FO", "CSV", "/NH"]
    ).decode("gbk", "ignore")
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2 and row[0].lower().startswith("illustrator"):
            return int(row[1])
    raise RuntimeError("Illustrator process not found")


def start_dialog_watcher(pages, range_xy, ok_xy, save_paths=None):
    """会话级静默对话框守护：自动处理 PDF 导入、存储为、以及各类确认弹窗。

    save_paths: 预期保存的绝对路径队列（按保存顺序）。出现「存储为」时把完整路径
    写入文件名框再点保存，避免误存到源文件位置。队列空时点取消以防覆盖源文件。
    """
    import win32gui, win32process, win32api, win32con
    pid = illustrator_pid()
    save_paths = list(save_paths or [])

    def enum():
        wins = []
        def cb(h, _):
            if win32gui.IsWindowVisible(h):
                _, p = win32process.GetWindowThreadProcessId(h)
                if p == pid:
                    wins.append(h)
        win32gui.EnumWindows(cb, None)
        return wins

    known = set(enum())
    stop = threading.Event()

    def click_rel(h, rx, ry):
        r = win32gui.GetWindowRect(h)
        px, py = r[0] + rx, r[1] + ry
        target = [h]
        def cb(c, _):
            if win32gui.IsWindowVisible(c):
                cr = win32gui.GetWindowRect(c)
                if cr[0] <= px < cr[2] and cr[1] <= py < cr[3]:
                    target.append(c)
        win32gui.EnumChildWindows(h, cb, None)
        tgt = target[-1]
        cr = win32gui.GetWindowRect(tgt)
        lp = ((py - cr[1]) << 16) | ((px - cr[0]) & 0xFFFF)
        win32api.PostMessage(tgt, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
        win32api.PostMessage(tgt, win32con.WM_LBUTTONUP, 0, lp)

    def buttons(h):
        out = []
        def cb(c, _):
            if win32gui.GetClassName(c) == "Button":
                out.append((c, win32gui.GetWindowText(c)))
        win32gui.EnumChildWindows(h, cb, None)
        return out

    def click_btn(h, keys):
        for c, t in buttons(h):
            if any(k in t for k in keys):
                win32gui.SendMessage(c, win32con.BM_CLICK, 0, 0)
                return t
        return None

    def edits(h):
        out = []
        def cb(c, _):
            if win32gui.GetClassName(c) == "Edit":
                out.append(c)
        win32gui.EnumChildWindows(h, cb, None)
        return out

    def confirm(h):
        title = win32gui.GetWindowText(h)
        if win32gui.GetClassName(h) != "#32770":
            return
        time.sleep(0.5)
        # 1) PDF 导入对话框：填范围 + 点确定（DroverLord 代理控件需坐标点击）
        if "PDF" in title:
            es = edits(h)
            if es:
                es.sort(key=lambda c: win32gui.GetWindowRect(c)[1])
                rng = es[-1]
                click_rel(h, *range_xy)
                time.sleep(0.3)
                win32gui.SendMessage(rng, 0x00B1, 0, -1)
                for ch in "1-%d" % pages:
                    win32gui.SendMessage(rng, win32con.WM_CHAR, ord(ch), 0)
                time.sleep(0.3)
            click_rel(h, *ok_xy)
            print("watcher: PDF 导入范围 1-%d 已确认" % pages)
            return
        # 2) 存储为 / Save As：写入预期完整路径再点保存（需有文件名框与保存键，否则视为报错弹窗走通用确认）
        bs = buttons(h)
        is_saveas = (("存储为" in title) or ("Save As" in title) or ("存储" in title)) \
            and any(("保存" in t) or ("Save" in t) for _, t in bs)
        if is_saveas:
            es = edits(h)
            if save_paths and es:
                path = save_paths.pop(0)
                win32gui.SendMessage(es[0], win32con.WM_SETTEXT, 0, path)
                time.sleep(0.3)
                t = click_btn(h, ("保存", "Save"))
                print("watcher: 存储为 -> %s (%s)" % (os.path.basename(path), t))
            else:
                t = click_btn(h, ("取消", "Cancel"))
                print("watcher: 存储为无预期路径，已取消 (%s)" % t)
            return
        # 3) 其它确认弹窗（字体/链接/色彩/警告等）：点默认确认键
        t = click_btn(h, ("确定", "OK", "继续", "是", "Yes", "Continue"))
        if t:
            print("watcher: 确认弹窗「%s」-> %s" % (title, t))

    def watch():
        handled = set()
        while not stop.is_set():
            for h in enum():
                if h in known or h in handled:
                    continue
                handled.add(h)
                confirm(h)
            stop.wait(0.15)
    threading.Thread(target=watch, daemon=True).start()
    return stop


def inject_intent(target_pdf, source_pdf):
    script = os.path.join(HERE, "inject_output_intent.py")
    r = subprocess.run([sys.executable, script, "--target", target_pdf, "--source", source_pdf],
                       capture_output=True, text=True, encoding="utf-8")
    msg = (r.stdout or r.stderr).strip()
    if r.returncode == 0:
        print("output intent:", msg)
    else:
        print("output intent: 跳过（%s）" % msg)


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input")
    ap.add_argument("--model")
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--rule", default="diff")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--bleed", default="auto", choices=["auto", "bleed", "trim"])
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--codes-csv", default=None)
    ap.add_argument("--no-audit", action="store_true")
    ap.add_argument("--catalog", default=DEFAULT_CATALOG)
    ap.add_argument("--dlg-range", default="130,283")
    ap.add_argument("--dlg-ok", default="208,462")
    a = ap.parse_args()

    catalog = load_catalog(a.catalog)
    if a.list_models:
        print("模版 -> 使用该模版的型号")
        print(describe(catalog))
        return
    if not a.input or not a.model:
        ap.error("--input 与 --model 必填（或 --list-models）")

    import pymupdf
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

    src = os.path.abspath(a.input)
    mode = "ai" if os.path.splitext(src)[1].lower() == ".ai" else "pdf"
    proj = os.path.dirname(os.path.dirname(src))
    outdir = os.path.abspath(a.outdir or os.path.join(proj, "output"))
    workdir = os.path.abspath(a.workdir or os.path.join(proj, "work"))
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(workdir, exist_ok=True)

    key, meta = resolve_model(catalog, a.model)
    pages, sizes, frame_pages = scan_input(src, meta["trim"])
    target, src_state, notes = choose_target(catalog, meta, a.bleed, sizes, frame_pages, pages)
    n = min(pages, a.limit) if a.limit else pages
    sw, sh, _ = sizes[0]
    print("型号 %s（%d位, 模版 %s）输入 %s: %d页 %.2fx%.2fmm -> %s版"
          % (key, meta["bits"], meta["template"], mode, pages, sw, sh, state_label(target)))
    for nt in notes:
        print("  [注意]", nt)

    try:
        codes, segs = rules.codes_with_start(n, a.rule, a.start, meta["max_code"])
    except ValueError as e:
        raise SystemExit("[规则错误] %s" % e)
    g = geometry(catalog, meta, target)
    frames = count_frames(src, meta["trim"], n)
    expect_deleted = 2 * n + frames

    def fmt(lst):
        return ",".join("%.4f" % v for v in lst)

    lay = build_jsx("lay_codes.jsx",
                    NEW_W="%.4f" % g["NEW_W"], NEW_H="%.4f" % g["NEW_H"],
                    TRIM_W="%.4f" % g["TRIM_W"], TRIM_H="%.4f" % g["TRIM_H"],
                    LEFTS=fmt(g["LEFTS"]), WIDTHS=fmt(g["WIDTHS"]),
                    BITOF=",".join(str(b) for b in g["BITOF"]),
                    TOP_OFF="%.4f" % g["TOP_OFF"], BAR_H="%.4f" % g["BAR_H"],
                    NBARS=g["NBARS"], CODES=",".join(str(c) for c in codes))
    open(os.path.join(workdir, "lay_run.jsx"), "w", encoding="utf-8").write(lay)

    import pythoncom, win32com.client
    pythoncom.CoInitialize()
    app = win32com.client.Dispatch("Illustrator.Application")
    app.UserInteractionLevel = 2

    t = time.time()
    rx, ry = (int(v) for v in a.dlg_range.split(","))
    ox, oy = (int(v) for v in a.dlg_ok.split(","))
    if mode == "ai":
        save_paths = [os.path.join(outdir, "_tmp_coded.ai"),
                      os.path.join(outdir, "_tmp_outlined.pdf")]
    else:
        save_paths = [os.path.join(outdir, "_tmp_coded.pdf")]
    watcher = start_dialog_watcher(n, (rx, ry), (ox, oy), save_paths)
    doc = app.Open(src)
    print("opened", doc.Name, "artboards:", doc.Artboards.Count, "%.1fs" % (time.time() - t))

    t = time.time()
    report = app.DoJavaScript(lay)
    print("process %.1fs:" % (time.time() - t), report[:300])
    parts = dict(kv.split("=", 1) for kv in report.split(";"))
    got_codes = [int(x) for x in parts["codes"].split(",")]
    if got_codes != codes:
        doc.Close(2)
        raise SystemExit("GATE FAILED: codes mismatch")
    if int(parts["deletedMarks"]) != expect_deleted:
        print("  [warn] deletedMarks=%s 预期=%s（以结构审计残留检查为准）"
              % (parts["deletedMarks"], expect_deleted))
    print("gate: codes ok, marks=%s (2*%d+%d frames)" % (parts["deletedMarks"], n, frames))

    base = a.name or (os.path.splitext(os.path.basename(src))[0] + "_铺码")
    base = state_suffix(base, target)
    rng = "1-%d" % n
    renames, pdf_outputs = [], []
    if mode == "ai":
        # 先存可编辑 .ai，再转曲，再导转曲 PDF（保留 .ai 文本可编辑）
        r = app.DoJavaScript(build_jsx("save_ai.jsx", AI_PATH=os.path.join(outdir, "_tmp_coded.ai")))
        print("save ai:", r)
        renames.append(("_tmp_coded.ai", base + ".ai"))
        ol = app.DoJavaScript(build_jsx("outline.jsx"))
        print("outline:", ol)
        if not ol.startswith("remaining=0"):
            doc.Close(2)
            raise SystemExit("GATE FAILED: 转曲后仍有文本对象 %s" % ol)
        r = app.DoJavaScript(build_jsx("save_pdf.jsx", PDF_PATH=os.path.join(outdir, "_tmp_outlined.pdf"), RANGE=rng))
        print("save pdf:", r)
        renames.append(("_tmp_outlined.pdf", base + "_转曲.pdf"))
        pdf_outputs.append(base + "_转曲.pdf")
    else:
        r = app.DoJavaScript(build_jsx("save_pdf.jsx", PDF_PATH=os.path.join(outdir, "_tmp_coded.pdf"), RANGE=rng))
        print("save pdf:", r)
        renames.append(("_tmp_coded.pdf", base + ".pdf"))
        pdf_outputs.append(base + ".pdf")

    doc.Close(2)
    watcher.set()
    final_pdfs = []
    for tmp, final in renames:
        sp, dp = os.path.join(outdir, tmp), os.path.join(outdir, final)
        if os.path.exists(sp):
            if os.path.exists(dp):
                os.remove(dp)
            os.rename(sp, dp)
            print("output:", final, os.path.getsize(dp), "bytes")
            if final.endswith(".pdf"):
                final_pdfs.append(dp)
        else:
            print("MISSING OUTPUT:", tmp)

    for dp in final_pdfs:
        inject_intent(dp, src)

    if a.codes_csv:
        with open(a.codes_csv, "w", encoding="utf-8-sig", newline="") as f:
            f.write("page,card,code,side\n")
            for i, c in enumerate(codes):
                p = i + 1
                f.write("%d,%d,%d,%s\n" % (p, (p + 1) // 2, c, "front" if p % 2 else "back"))
        print("codes csv:", a.codes_csv)

    if not a.no_audit:
        import verify as verify_mod
        import audit as audit_mod
        print()
        ok = True
        for dp in final_pdfs:
            good, msg = verify_mod.self_check(dp, key, a.rule, a.start, catalog, expect=target)
            print("自检:", os.path.basename(dp), msg, "OK" if good else "FAIL")
            ok &= good
            audit_mod.audit(dp, key, a.rule, catalog, start=a.start)  # 结构层参考诊断
        if not ok:
            raise SystemExit("像素硬门槛未通过，不得交付（见上方自检）")
    print("done", mode)


if __name__ == "__main__":
    main()
