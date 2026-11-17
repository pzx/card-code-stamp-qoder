/* 转曲：把当前文档所有文本转轮廓（.ai 已先存好保留可编辑），返回 remaining=未转曲文本数 */
(function () {
    var doc = app.activeDocument;
    var before = 0, outlined = 0, guard = 0;
    try { before = doc.textFrames.length; } catch (e0) {}
    try {
        while (doc.textFrames.length > 0 && guard++ < 20000) {
            var tf = doc.textFrames[0];
            try { tf.createOutline(); outlined++; }
            catch (eO) {
                try { tf.selected = true; app.executeMenuCommand("outline"); outlined++; }
                catch (eM) { break; }
            }
        }
    } catch (eL) {}
    var after = 0;
    try { after = doc.textFrames.length; } catch (e1) {}
    return "remaining=" + after + ";before=" + before + ";outlined=" + outlined;
})();
