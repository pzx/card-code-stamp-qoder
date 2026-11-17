/* Parametric code laying: resize artboards, remove crop marks + trim frames, draw bars.
   Placeholders filled by stamp.py: NEW_W NEW_H TRIM_W TRIM_H LEFTS WIDTHS BITOF TOP_OFF BAR_H NBARS CODES */
(function () {
    var doc = app.activeDocument;
    var NEW_W = {{NEW_W}}, NEW_H = {{NEW_H}};
    var TRIM_W = {{TRIM_W}}, TRIM_H = {{TRIM_H}};
    var lefts = [{{LEFTS}}], widths = [{{WIDTHS}}], bitOf = [{{BITOF}}];
    var topOff = {{TOP_OFF}}, barH = {{BAR_H}}, nBars = {{NBARS}};
    var codes = [{{CODES}}];
    var report = [];
    var nAB = doc.artboards.length;
    // 试跑(--limit)时码值数少于画板数：只处理前 N 个画板，多余画板保持原样（不删画板，避免触发存储对话框）
    var n = Math.min(nAB, codes.length);

    var orig = [];
    for (var i = 0; i < nAB; i++) orig.push(doc.artboards[i].artboardRect);
    var ow = orig[0][2] - orig[0][0], oh = orig[0][1] - orig[0][3];

    function spansFull(b) { return (b[2] - b[0]) > ow * 0.9 && (b[1] - b[3]) > oh * 0.9; }
    function isTrimFrame(b) {
        return Math.abs((b[2] - b[0]) - TRIM_W) < 1.5 && Math.abs((b[1] - b[3]) - TRIM_H) < 1.5;
    }
    var toDelete = [];
    var cps = doc.compoundPathItems;
    for (var i = 0; i < cps.length; i++) {
        try {
            var cp = cps[i];
            if ((spansFull(cp.geometricBounds) || isTrimFrame(cp.geometricBounds)) && cp.pathItems.length > 0) {
                var ch = cp.pathItems[0];
                if (ch.stroked && !ch.filled && !ch.clipping) toDelete.push(cp);
            }
        } catch (e) {}
    }
    var ps = doc.pathItems;
    for (var i = 0; i < ps.length; i++) {
        try {
            var p = ps[i];
            if (p.stroked && !p.filled && !p.clipping && (spansFull(p.geometricBounds) || isTrimFrame(p.geometricBounds))) toDelete.push(p);
        } catch (e) {}
    }
    var deleted = 0;
    for (var i = 0; i < toDelete.length; i++) {
        try { toDelete[i].remove(); deleted++; } catch (e) {}
    }
    report.push("deletedMarks=" + deleted);

    for (var i = 0; i < n; i++) {
        var r = orig[i];
        var cx = (r[0] + r[2]) / 2, cy = (r[1] + r[3]) / 2;
        doc.artboards[i].artboardRect = [cx - NEW_W / 2, cy + NEW_H / 2, cx + NEW_W / 2, cy - NEW_H / 2];
    }

    for (var i = 0; i < n; i++) {
        var code = codes[i];
        var r = doc.artboards[i].artboardRect;
        var L = r[0], B = r[3];
        for (var p = 0; p < nBars; p++) {
            var bit = (code >> bitOf[p]) & 1;
            var rect = doc.pathItems.rectangle(B + topOff, L + lefts[p], widths[p], barH);
            rect.stroked = false;
            rect.filled = true;
            var c = new CMYKColor();
            c.cyan = 0; c.magenta = 0; c.yellow = 0; c.black = bit ? 100 : 0;
            rect.fillColor = c;
            try { rect.overprint = false; } catch (eOv) {}  // 挖空覆盖，禁叠印（否则与背景混色扫码失效）
        }
    }
    report.push("artboards=" + n);
    report.push("codes=" + codes.join(","));
    return report.join(";");
})();
