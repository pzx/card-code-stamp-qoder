#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""catalog 读取与型号解析（stamp.py / verify.py 共用）。

型号写法容错：
  * 大小写无关：V7c / v7C / P6B 都能命中 v7c / p6b
  * 全角、空格、下划线、各种连字符统一成半角 '-'
  * 大卡系列：p6大卡 / p6-大卡 / p6(大卡) -> p6；p6小卡 / p6-小卡 -> p6-小卡
  * 命中"未确认模版归属"的型号时给出警告（仍可用，但不静默）
"""
import difflib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CATALOG = os.path.join(HERE, os.pardir, "references", "catalog.json")

_TABLE = str.maketrans({"－": "-", "—": "-", "–": "-", "＿": "-", "\u3000": " ",
                        "（": "(", "）": ")", "　": " "})
SMALL = "小卡"
LARGE = "大卡"


def load_catalog(path=None):
    with open(path or DEFAULT_CATALOG, encoding="utf-8") as f:
        return json.load(f)


def normalize(name):
    s = str(name).strip().translate(_TABLE).lower()
    s = s.replace(" ", "").replace("_", "-")
    # 括号/斜杠等描述性包裹去掉：p6(大卡) -> p6大卡
    for ch in "()[]{}":
        s = s.replace(ch, "")
    # 大卡 = 默认型号（catalog 里的裸名字就是大卡），把标记去掉
    for tag in ("-" + LARGE, LARGE):
        s = s.replace(tag, "")
    # 小卡统一成 '-小卡' 后缀
    if SMALL in s:
        s = s.replace("-" + SMALL, "").replace(SMALL, "") + "-" + SMALL
    s = s.replace("--", "-").strip("-")
    # 可能的别名：p6b-大卡 -> p6b 已在上一步处理
    return s


def resolve_model(catalog, name):
    """返回 (规范型号键, meta)；失败抛 SystemExit 并给出候选提示。"""
    models = catalog["models"]
    key = normalize(name)
    if key in models:
        meta = models[key]
        if meta.get("template_confirmed") is False:
            sys.stderr.write("[warn] 型号 %s 的模版归属未确认，按 %s 处理；"
                             "如不对请先确认再交付\n" % (name, meta["template"]))
        return key, meta

    # 常见别名再兜一层
    alias = {"v7": "v7c", "p5b": "p6b", "p6b-card": "p6b-小卡"}
    if key in alias and alias[key] in models:
        return alias[key], models[alias[key]]

    cand = difflib.get_close_matches(key, list(models), n=4, cutoff=0.5)
    hint = ("，最接近: %s" % ", ".join(cand)) if cand else ""
    raise SystemExit("未知型号 %r（解析为 %r）%s\n可用型号: %s"
                     % (name, key, hint, ", ".join(sorted(models))))


def template_of(catalog, model):
    """返回 (template_key, template, meta)"""
    key, meta = resolve_model(catalog, model)
    return meta["template"], catalog["templates"][meta["template"]], meta


STATE_LABEL = {"bleed": "出血", "trim": "未出血", "marked": "大版(带裁切框)"}


def state_label(state):
    return STATE_LABEL.get(state, "未知尺寸")


def size_state(meta, w_mm, h_mm, tol=0.8):
    """判定一个页面尺寸属于 出血版 / 未出血版 / 都不符。

    型号的几何约定（catalog）：
      trim  = 未出血成品尺寸（如 60x90）
      bleed = 出血尺寸 = trim 四边各 +3mm（如 66x96）

    返回 (state, info)：
      state = "bleed" | "trim" | None
      info  = dict(trim=..., bleed=..., d_trim=..., d_bleed=..., near="bleed"|"trim",
                   bleed_mm=每边出血量, exact=是否落在容差内)
    d_trim / d_bleed 是"与哪一侧更接近"的度量（毫米，两轴之和），
    用于尺寸既不是出血也不是未出血时（例如导出时多留了 0.5mm）给出最接近的一侧。
    """
    tw, th = meta["trim"]
    bw, bh = meta["bleed"]
    d_trim = abs(w_mm - tw) + abs(h_mm - th)
    d_bleed = abs(w_mm - bw) + abs(h_mm - bh)
    if abs(w_mm - bw) <= tol and abs(h_mm - bh) <= tol:
        state = "bleed"
    elif abs(w_mm - tw) <= tol and abs(h_mm - th) <= tol:
        state = "trim"
    else:
        state = None
    return state, dict(trim=(tw, th), bleed=(bw, bh), d_trim=round(d_trim, 3),
                       d_bleed=round(d_bleed, 3),
                       near=("bleed" if d_bleed <= d_trim else "trim"),
                       bleed_mm=round((bw - tw) / 2, 3), exact=state is not None)


def describe(catalog):
    """型号 -> 模版 的分组说明，用于打印/自查（同时列出未出血/出血两套尺寸）"""
    lines = []
    for tk, t in catalog["templates"].items():
        members = ", ".join(t.get("models") or [])
        pending = t.get("pending_models") or []
        tw, th = t["page_w"], t["page_h"]
        lines.append("%-16s 未出血 %3.0fx%-4.0f  出血 %3.0fx%-4.0f  %2d槽  %s%s"
                     % (tk, tw, th, tw + 6, th + 6, len(t["slots"]), members,
                        ("  [待确认: %s]" % ", ".join(pending)) if pending else ""))
    return "\n".join(lines)
