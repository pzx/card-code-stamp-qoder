#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""铺码规则解析与码值生成（stamp.py / verify.py 共用）。

设计原则：宁可报错，不猜语义。
  * 段必须显式给出页码范围，且覆盖 1..N、互不重叠、不留空档
  * diff 段（正反不同）长度必须为偶数，否则该段首尾同一张卡只有一面
  * 混合规则默认"接续编号"：第二段接着第一段往下排，不回到 1
  * 要强制指定某段起点写 `diff:1-40=101`（此时不会再自动接续）
  * 全量码值必须不超位宽上限（10 位 = 1023），超限即终止

支持的写法：
    diff                        全书正反不同
    same                        全书正反相同
    diff:1-40,same:41-100       混合（按段连续编号）
    diff:1-40=101,same:41-100   混合，且第一段码值从 101 开始
"""
import re

MODES = {"diff": "diff", "front_back_diff": "diff", "正反不同": "diff",
         "same": "same", "front_back_same": "same", "正反相同": "same"}

SEG_RE = re.compile(r"^(?P<mode>[a-z_正反不同]+)\s*[:：]\s*(?P<a>\d+)\s*[-–—~]\s*(?P<b>\d+)"
                    r"(?:\s*[=@]\s*(?P<start>\d+))?$", re.I)


def parse(spec):
    """返回 ('whole', mode) 或 ('segments', [dict(mode,a,b,start)])"""
    s = (spec or "").strip().lower()
    if s in MODES:
        return "whole", MODES[s]

    segs = []
    for raw in re.split(r"[,;，；]", s):
        raw = raw.strip()
        if not raw:
            continue
        m = SEG_RE.match(raw)
        if not m:
            raise ValueError("无法解析规则片段 %r（应形如 diff:1-40 或 same:41-100=201）" % raw)
        mode = MODES.get(m.group("mode"))
        if not mode:
            raise ValueError("未知模式 %r（只支持 diff / same）" % m.group("mode"))
        a, b = int(m.group("a")), int(m.group("b"))
        if b < a:
            raise ValueError("片段 %r 结束页小于起始页" % raw)
        segs.append(dict(mode=mode, a=a, b=b,
                         start=int(m.group("start")) if m.group("start") else None))

    if not segs:
        raise ValueError("规则为空")
    segs.sort(key=lambda d: d["a"])

    # 重叠 / 覆盖检查
    for prev, cur in zip(segs, segs[1:]):
        if cur["a"] <= prev["b"]:
            raise ValueError("片段重叠：%d-%d 与 %d-%d" % (prev["a"], prev["b"], cur["a"], cur["b"]))
        if cur["a"] != prev["b"] + 1:
            raise ValueError("片段不连续：%d-%d 与 %d-%d 之间缺页"
                             % (prev["a"], prev["b"], cur["a"], cur["b"]))
    if segs[0]["a"] != 1:
        raise ValueError("第一段必须从第 1 页开始，当前为 %d" % segs[0]["a"])
    return "segments", segs


def codes(n, spec, max_code=None):
    """生成每页码值（1 起）。返回 (codes, segs)；不合法直接抛 ValueError。"""
    kind, data = parse(spec)
    if kind == "whole":
        if data == "diff":
            out = [p + 1 if p % 2 == 1 else p - 1 for p in range(1, n + 1)]
            segs = [dict(mode="diff", a=1, b=n, start=1)]
        else:
            out = [(p + 1) // 2 for p in range(1, n + 1)]
            segs = [dict(mode="same", a=1, b=n, start=1)]
    else:
        segs = data
        if segs[-1]["b"] != n:
            raise ValueError("规则只覆盖到第 %d 页，但文件有 %d 页（必须全覆盖）"
                             % (segs[-1]["b"], n))
        out = [None] * n
        next_base = 1                      # 默认接续编号
        for seg in segs:
            length = seg["b"] - seg["a"] + 1
            if seg["mode"] == "diff" and length % 2:
                raise ValueError("正反不同片段 %d-%d 长度为 %d（必须为偶数，否则有卡只有一面）"
                                 % (seg["a"], seg["b"], length))
            base = seg["start"] if seg["start"] is not None else next_base
            for i in range(length):
                p = seg["a"] + i
                if seg["mode"] == "diff":
                    # 段内第 i 页：奇数位 = 2k，偶数位 = 2k-1
                    k = i // 2 + 1
                    out[p - 1] = base - 1 + (2 * k if i % 2 == 0 else 2 * k - 1)
                else:
                    out[p - 1] = base + i // 2
            next_base = max(out[seg["a"] - 1:seg["b"]]) + 1

    bad = [(i + 1, c) for i, c in enumerate(out) if c is None or c < 0]
    if bad:
        raise ValueError("存在未赋码页: %s" % bad[:8])
    if max_code is not None:
        over = [(i + 1, c) for i, c in enumerate(out) if c > max_code]
        if over:
            raise ValueError("码值超 %d 位上限 %d: %s（超限会被静默截断成别的码，必须终止）"
                             % (len(bin(max_code)) - 2, max_code, over[:8]))
    _check_unique(out)
    return out, segs


def _check_unique(out):
    """正反不同时同一码最多出现 2 次（一卡两面）；相同规则最多 2 次；
    出现 3 次以上说明分段编号撞车。"""
    seen = {}
    for i, c in enumerate(out):
        seen.setdefault(c, []).append(i + 1)
    dup = {c: ps for c, ps in seen.items() if len(ps) > 2}
    if dup:
        k, ps = sorted(dup.items())[0]
        raise ValueError("码值 %d 在页 %s 出现 %d 次（同一码只能属于一张卡的两面），"
                         "说明分段起始码重叠" % (k, ps[:6], len(ps)))


def codes_with_start(n, spec, start=1, max_code=None):
    """codes() 的包装：支持全局 --start N（默认 1）。

    整书规则（diff/same）转成带 =起点 的分段写法；分段规则仅当首段未显式
    指定起点时把 --start 作用于首段。stamp.py 与 verify.py 共用，保证期望值一致。
    """
    if start and start != 1:
        kind, data = parse(spec)
        if kind == "whole":
            spec = "%s:1-%d=%d" % (data, n, start)
        elif data[0].get("start") is None:
            data[0]["start"] = start
            spec = ",".join("%s:%d-%d%s" % (s["mode"], s["a"], s["b"],
                            ("=%d" % s["start"]) if s.get("start") else "") for s in data)
    return codes(n, spec, max_code)


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    spec = sys.argv[2] if len(sys.argv) > 2 else "diff"
    print(codes(n, spec, 1023))
