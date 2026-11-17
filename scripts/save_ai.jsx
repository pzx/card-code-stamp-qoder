var doc = app.activeDocument;
var o = new IllustratorSaveOptions();
var log = [];
function trySet(fn, name) { try { fn(); log.push(name + "=ok"); } catch (e) { log.push(name + "=ERR:" + e.message); } }
trySet(function () { o.embedLinkedFiles = true; }, "embedLinkedFiles");
trySet(function () { o.PDFCompatible = true; }, "PDFCompatible");
doc.saveAs(new File("{{AI_PATH}}"), o);
log.join(",");
