---
name: card-code-stamp-qoder
description: 卡片铺黑白码自动化（Illustrator JSX 静默管线 + 印刷安全校验）。对设计 PDF/AI 判定型号、去除裁切线与黑色裁切框、裁切到出血或未出血尺寸，在页面底部最上层铺黑白码（CMYK 纯黑/纯白、禁叠印），输出印刷 PDF；AI 输入另输出 .ai 与转曲 PDF，并注入 FOGRA39 输出意图。像素级硬门槛校验码值/尺寸/残留，结构审计作参考。全程静默自动处理 PDF 导入、存储为与各类确认弹窗，无需人工点击。当用户提到：铺码、黑白码、码值、条码、出血、去黑框、卡片印刷文件处理、型号 p7/p6b/p6/p5/v2/v7c/v3/s7/v11/小卡，或要求静默/自动处理 Illustrator 时使用。
---

# 卡片铺黑白码（Illustrator 静默管线）

把多页设计稿（PDF / AI）处理成带黑白码的印刷文件。对象级修改全部在 Illustrator JSX 内完成，Python 负责几何/码值计算、静默驱动与校验。

## 硬性规格

- 黑=CMYK 0/0/0/100，白=0/0/0/0，**不做色彩转换**；码带最上层、贴页底、按模版几何横排
- 默认「正反不同」：页1=码2、页2=码1……；起始码默认 1
- 码值上限=2^码位−1：10 位 **0–1023**、8 位 0–255、6 位 0–63（超上限报错终止）
- V3 位序特殊：左→右 bit9,bit7,bit6,bit5,bit4,bit3,bit2,bit1,bit0,bit8；其余型号 bit9…bit0 顺排
- 去除裁切线**和黑色裁切框**；PDF 输入→只输出 PDF，AI 输入→.ai + 转曲 PDF

完整尺寸/型号/位序/几何换算见 [references/models.md](references/models.md)；实测几何在 [references/catalog.json](references/catalog.json)。

## 环境

- Windows + Adobe Illustrator（2024 验证）+ Python 3.8+
- 依赖：`pip install pymupdf pywin32`
- 目录约定：`<项目>/input/`（待处理）、`<项目>/output/`（交付）、`<项目>/work/`（中间 jsx）

## 工作流

### 1. 判定型号与出血
读输入首页尺寸/描边框，对照 models.md 规格表定型号；型号名支持容错（`--list-models` 列出）。`--bleed auto` 按源页更接近出血或未出血自动裁切（严格判定，偏差>3mm 拒绝）。

### 2. 铺码（静默）
```bash
python <skill>/scripts/stamp.py --input <绝对路径>/xx.pdf --model v3 --rule diff \
    [--start 1] [--bleed auto] [--outdir <绝对路径>] [--name 自定义名] [--limit 6] [--codes-csv codes.csv]
```
**所有路径必须绝对**（相对路径会让 Illustrator 按自身目录解析并弹「存储为」）。
脚本自动：生成参数化 jsx → COM 打开 → 删标记/黑框 → 缩放画板 → 画码（禁叠印）→ gate 校验 → 保存 → 改中文名 → 注入 FOGRA39 输出意图 → 像素自检。
- 会话级 watcher 自动处理：PDF 导入（填范围 `1-N`+确定）、**存储为**（写入预期绝对路径+保存）、其它确认弹窗（确定/继续/是）
- `--rule`：`diff` / `same` / 混合 `diff:1-40,same:41-100`（按卡序，严格覆盖/不重叠/diff 段偶数/接续编号）
- `--start N`：起始码；`--limit 6`：试跑 6 页（AI 模式保留全部画板、仅前 6 个铺码，PDF 只导 1-6）
- AI 输入：先存可编辑 `.ai` → `createOutline` 转曲（断言 remaining=0）→ 导 `_转曲.pdf`；PDF 输入只产 `.pdf`
- 输出名自动加 `_出血`/`_未出血` 后缀

### 3. 校验（双层）
stamp.py 结束自动跑像素硬门槛；也可单独：
```bash
python <skill>/scripts/verify.py --input <绝对路径>/xx.pdf --model v3 --rule diff [--start 1] [--strict]
```
- **硬门槛**（必须过）：尺寸/出血态、逐槽像素解码 mismatch=0、RGB 色偏=0、裁切标记残留=0、无异尺寸页
- **结构审计**（默认仅参考）：纯 CMYK、叠印 OP、几何±0.05mm、反解码值、字体。`--strict` 才作硬门槛
- 注意：底部含富黑/叠印背景 artwork 的设计（如 V3）会让结构审计误报，**以像素硬门槛为准**；干净设计可加 `--strict`

## 已知坑（务必遵守，详见 references/illustrator-automation.md）

| 坑 | 对策 |
|---|---|
| JSX 中文文件名让 Illustrator 崩溃 | ASCII 临时名保存，`doc.Close` 后 `os.rename` 改中文；JSX 非 ASCII 转 `\uXXXX` |
| COM Dispatch 保存选项对象失败 | `new PDFSaveOptions/IllustratorSaveOptions` 写在 JSX 内 |
| save_ai 设 artboardRange 拆成 100 个文件 | AI 保存**不**设 saveMultipleArtboards/artboardRange |
| PDF 导入「全部」模式得链接对象不可编辑 | 必须「范围」模式输入 `1-N`，得原生路径 |
| 导入/存储对话框需人工点击 | 会话级 watcher：PDF 导入填范围+确定；存储为写绝对路径+保存；其它确认弹窗点确定/继续/是 |
| **相对路径**触发「存储为」/存错位置 | 输入/outdir/workdir 及 JSX 保存路径一律 `os.path.abspath` |
| JSX 删画板(`artboard.remove`)触发「存储为」 | 试跑不删画板，仅循环前 N 个铺码/缩放 |
| 码条与背景叠印混色致扫码失效 | 画码后 `rect.overprint=false`（挖空覆盖） |
| 对话框坐标随版本/DPI 变 | 默认 (130,283)/(208,462)；`--dlg-range/--dlg-ok` 覆盖；进程设 SetProcessDpiAwareness(2) |
| 有的页含 2~3 个黑框 | gate 期望删除数=2*页数+输入PDF实测框数（动态） |
| 结构审计对富黑/叠印背景误报 | 像素硬门槛为准；审计默认参考，`--strict` 才硬判 |

Illustrator COM/JSX/静默导入的完整约束见 [references/illustrator-automation.md](references/illustrator-automation.md)。
