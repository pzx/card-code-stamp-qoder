var doc = app.activeDocument;
var o = new PDFSaveOptions();
var log = [];
function trySet(fn, name) { try { fn(); log.push(name + "=ok"); } catch (e) { log.push(name + "=ERR:" + e.message); } }
trySet(function () { o.saveMultipleArtboards = true; }, "saveMultipleArtboards");
trySet(function () { o.artboardRange = "{{RANGE}}"; }, "artboardRange");
trySet(function () { o.preserveEditability = false; }, "preserveEditability");
trySet(function () { o.trimMarks = false; }, "trimMarks");
trySet(function () { o.registrationMarks = false; }, "registrationMarks");
trySet(function () { o.colorBars = false; }, "colorBars");
trySet(function () { o.pageInformation = false; }, "pageInformation");
trySet(function () { o.generateThumbnails = false; }, "generateThumbnails");
trySet(function () { o.viewAfterSaving = false; }, "viewAfterSaving");
trySet(function () { o.pageMarksType = PageMarksType.None; }, "pageMarksType");
trySet(function () { o.compatibility = PDFCompatibility.ACROBAT_7; }, "compatibility");
doc.saveAs(new File("{{PDF_PATH}}"), o);
log.join(",");
