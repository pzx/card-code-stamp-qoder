# Illustrator 自动化实战要点（Windows / COM）

本技能用 pywin32 COM 驱动 Illustrator，所有对象级修改在 JSX 内完成。以下均为实战验证过的硬约束，违反会崩溃或产出错误文件。

## 1. COM 连接与交互级别

```python
import pythoncom, win32com.client
pythoncom.CoInitialize()
app = win32com.client.Dispatch("Illustrator.Application")
app.UserInteractionLevel = 2   # aiDisplayNone：抑制大部分弹窗
```

- 用 `app.DoJavaScript(jsx_string)` 执行脚本，**不要**用 `DoJavaScriptFile`（路径/编码问题多）。
- 取 Illustrator 进程 PID 用 `tasklist /FI "IMAGENAME eq Illustrator.exe" /FO CSV /NH`（GBK 解码）；`app.hWnd` 不存在。

## 2. 中文 / 非 ASCII 致命坑

- **JSX 字符串里的中文文件名会让 Illustrator 崩溃**（RPC 断开）。对策：保存一律用 ASCII 临时名（`_tmp_coded.ai` / `_tmp_coded_outlined.pdf`），`doc.Close(2)` 之后再用 `os.rename` 改成中文交付名。
- 传给 `DoJavaScript` 的 JSX 源码里所有非 ASCII 字符转成 `\uXXXX` 转义：
  ```python
  "".join(ch if ord(ch) < 128 else "\\u%04x" % ord(ch) for ch in jsx)
  ```
- JSX 里 `new File(path)` 的路径用**正斜杠**（Windows 反斜杠在 JS 字符串里是转义符）。替换占位符时 `v.replace("\\", "/")`。
- **所有路径必须绝对**（`os.path.abspath`）：输入、outdir、workdir、JSX 保存路径。相对路径会被 Illustrator 按自身当前目录（常为源文件目录）解析，触发「存储为」对话框甚至存错位置/覆盖源文件。

## 3. 保存选项必须在 JSX 内创建

`win32com.client.Dispatch("Illustrator.PDFSaveOptions")` / `IllustratorSaveOptions` 会报 "unspecified error"。把 `new PDFSaveOptions()` / `new IllustratorSaveOptions()` 写在 JSX 里，用 `trySet` 逐项设置（个别属性 ERR 无害）。

- **save_ai.jsx**：`embedLinkedFiles=true`、`PDFCompatible=true`。**绝不**设 `saveMultipleArtboards` / `artboardRange`——会把 100 画板拆成 100 个 `_tmp-NN.ai` 临时文件。
- **save_pdf.jsx**：`saveMultipleArtboards=true` + `artboardRange="1-N"`；`trimMarks/registrationMarks/colorBars/pageInformation/generateThumbnails/viewAfterSaving=false`。`pageMarksType`、`compatibility` 两项可能 ERR，无害。
- AI 模式顺序：**先存可编辑 `.ai`** → `outline.jsx` 转曲 → 再导 `_转曲.pdf`（保留 `.ai` 文本可编辑）。PDF 模式只导 `.pdf`。
- **不要删画板**（`artboard.remove()`）：会触发「存储为」对话框。试跑 `--limit` 时 JSX 只循环前 `min(画板数, 码值数)` 个铺码/缩放，多余画板保持原样。

## 4. PDF 导入静默处理（核心难点）

`app.Open(pdf)` 会弹「PDF 导入」对话框。必须后台线程自动处理，**不能让用户点击**。

### 范围模式 vs 全部模式（决定可否编辑）
- 选「全部」→ 每页变成**链接 PlacedItem**（`placed:100, pathItems:0`），不可编辑，铺码失败。
- 选「范围」并输入 `1-N` → 每页变成**原生可编辑路径**（`pathItems:16608, placed:0`）。**必须用范围模式**。

### 对话框是 Adobe 自绘 UI（DroverLord）
- 子 `Edit` 控件是空代理：`WM_SETTEXT` 无效、读回空文本。输入范围必须**逐字符 `WM_CHAR`**。
- 点击用 `PostMessage WM_LBUTTONDOWN/UP`，坐标相对窗口左上角；目标取包含该点的最深子窗口。
- 实测坐标（Illustrator 2024 中文版、100% DPI）：范围输入框 `(130,283)`，确定按钮 `(208,462)`。**换版本/DPI 需重新探针**（见 probe_dialog.py 思路：PrintWindow 截图 + EnumChildWindows 列结构）。

### watcher 线程逻辑（会话级，覆盖打开→保存全程）
```
pid = illustrator_pid()
known = 启动前已存在的该 PID 窗口集合
save_paths = 预期保存的绝对路径队列（AI:[_tmp_coded.ai,_tmp_outlined.pdf] / PDF:[_tmp_coded.pdf]）
循环 EnumWindows 找该 PID 的新可见 #32770 窗口：
  标题含 "PDF"        → 点范围框(130,283) → EM_SETSEL 全选 → WM_CHAR 逐字 "1-N" → 点确定(208,462)
  标题含 存储为/Save 且有「保存」键+文件名框 → WM_SETTEXT 写入 save_paths.pop(0) 绝对路径 → 点保存
                        （无预期路径则点取消，防覆盖源文件；报错弹窗无保存键→走通用确认）
  其它               → 点 确定/OK/继续/是/Yes（字体/链接/色彩/警告等确认）
doc.Close 后 stop.set() 结束线程（异常退出时 daemon 线程随进程结束）
```
- **务必忽略「进度」对话框**：它也是 #32770 但无上述按钮/标题特征，通用确认匹配不到键即跳过。
- 存储为的文件名框是标准 Win32 Edit，`WM_SETTEXT` 有效（与 PDF 导入的 DroverLord 代理不同）；写**完整绝对路径**可让对话框跳转目录。
- 进程开头 `ctypes.windll.shcore.SetProcessDpiAwareness(2)`（失败回退 `SetProcessDPIAware`），否则坐标/截图受 DPI 缩放错乱。
- 截图诊断用 `PrintWindow(hwnd, hdc, 2)`，不要截前台（会截到别的窗口）。

## 5. 删除裁切标记 / 黑框（JSX 内）

用 `doc.compoundPathItems` 与 `doc.pathItems` 两个循环，`try/catch` 包裹每项，条件：`stroked && !filled && !clipping` 且几何匹配：
- **裁切标记**：包围盒跨度 > 原画板宽高的 90%（`spansFull`，阈值由 JSX 读 `orig[0]` 动态算）。
- **黑色裁切框**：包围盒 ≈ 未出血尺寸（如 V3 105×160mm，容差 1.5pt）。注意有的页有 2~3 个框，gate 要按输入 PDF 实际框数动态算。

先收集到 `toDelete` 再统一 `remove()`，避免遍历中修改集合。本管线此法稳定（与「PDF 内容流手术」方案不同，二者皆可）。

## 6. 画板resize与画码

- 读各画板原 `artboardRect`，按中心缩放到目标尺寸：`[cx-W/2, cy+H/2, cx+W/2, cy-H/2]`。
- 画码：`doc.pathItems.rectangle(B+topOff, L+lefts[p], widths[p], barH)`，`stroked=false, filled=true`，`CMYKColor` black=bit?100:0。L/B 取 resize 后画板左/底。
- **禁叠印**：画完设 `rect.overprint=false`（挖空覆盖）。否则码条与底部富黑/叠印背景混色，扫码失效。
- 试跑保护：`n=min(画板数, codes.length)`，resize/画码循环只到 n，避免在多余画板画 undefined 码。

## 7. 交付 gate（必须）

JSX 返回 `deletedMarks=N;artboards=N;codes=...`。Python 侧：
- 期望码值表按规则生成，逐页比对。
- 期望删除数 = `2*页数 + 输入PDF黑框数`（黑框用 pymupdf 数：`type=="s"`、宽≈未出血宽、高≈未出血高、`len(items)==8` 的描边矩形）。
- 不符 → `doc.Close(2)` 并报错终止，不产出。

## 8. 转曲、输出意图与双层校验

- **转曲**：`outline.jsx` 循环 `doc.textFrames[0].createOutline()`（guard 20000，失败回退 `executeMenuCommand("outline")`），返回 `remaining=N`。Python 断言 `remaining=0`，否则终止。源稿若本就无文本则 before=0 属正常。
- **输出意图**：保存后用 `inject_output_intent.py` 从源文件复用 ICC（FOGRA39）注入交付 PDF。PDF 源通常带 OutputIntent 可注入；`.ai` 的 PDF 兼容层常无 OutputIntent → 优雅跳过（打印警告，不致命）。
- **双层校验**（verify.py / stamp.py 自检）：
  - 硬门槛：尺寸/出血态（`catalog_util.size_state`，容差 0.8mm、>3mm 拒绝）、逐槽中心像素解码 mismatch=0、RGB 色偏 casts=0、裁切标记残留=0、无异尺寸页。像素层等价扫码枪视角，是可靠判据。
  - 结构审计（audit.py，默认参考）：内容流纯 CMYK、叠印 OP、几何±0.05mm、反解码、字体。**对底部含富黑/叠印背景 artwork 的设计（如 V3）会误报**（把背景富黑当码条颜色），故不作硬门槛；干净设计可 `--strict` 提升为硬判。
