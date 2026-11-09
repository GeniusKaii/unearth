# 拾遗 unearth

从旧硬盘里找回被删掉、被忘掉的照片和视频，然后按时间、相机、地点、分辨率筛成一条能翻的时间线。

名字取自"路不拾遗"——把别人（也包括过去的自己）丢在硬盘角落里的东西捡回来。

```
扫描  →  雕刻出能解析的完整文件（照片/视频/文档）
     →  NTFS 删除记录 → 原始文件名 + 拍摄/修改时间
     →  打分 / 去重 / 时间线
     →  一个双击就能翻的离线相册，收藏、筛选、导出清单
```

## 它和"数据恢复软件"有什么不一样

恢复软件给你一堆 `IMG_0001.JPG`，一万个，你不知道从哪看起。
unearth 的第二半是**筛选**：按天分组的相册、相机型号、GPS、像素、推荐度、重复项隐藏、收藏导出。
第一半才是恢复：分区签名雕刻 + NTFS 删除记录（保留原文件名和原始目录，比如 `DCIM/100CANON/IMG_1234.JPG`）。

## 三条铁律

1. **只读**。源设备只用 `GENERIC_READ` 打开，代码里没有任何写源盘的路径。
2. **输出到另一块盘**。工具会拒绝把输出写进源分区。
3. **先别往源盘写任何东西**。删掉的文件只是"还没被覆盖"；插上盘之后别在里面建文件、别整理磁盘、别让系统给它做索引。要更保险，先用 dd / HDDRawCopy 做整盘镜像，然后扫镜像（`img:路径`），原盘就可以随便动了。

## 安装

只要 Python 3.9+，零第三方依赖：

```powershell
cd D:\code\unearth
python -m pip install -e .
unearth --version
```

不想装也行，直接用模块方式跑（下文命令把 `unearth` 换成 `python -m unearth` 即可）：

```powershell
cd D:\code\unearth
python -m unearth --version
```

可选增强（不装也能跑，只是少一些缩略图）：

- `Pillow` — 给没有内嵌预览图的 JPEG 生成缩略图
- `ffmpeg` — 给视频抽一帧当封面

## 快速开始

**第 0 步：先自检，不碰真盘。** 用假镜像跑一遍全流程：

```powershell
cd D:\code\unearth
python -m unearth selftest --size 24M
```

看到 `结果：16/16 全部通过。工具可用。` 再往下走。

**第 1 步：看盘。** 用管理员 PowerShell（右键 → 以管理员身份运行）：

```powershell
cd D:\code\unearth
python -m unearth drives
```

普通终端也能看到卷和文件系统，但读裸设备必须管理员。旧盘一般是 `vol:E:` 这种；整块盘是 `disk:2`。

**第 2 步：成批雕刻。** 输出目录必须在**另一块盘**上，且空间要够（建议 ≥ 源盘已用空间的一半）：

```powershell
python -m unearth scan vol:E: -o D:\dig\e --profile all --exclude-live
```

- `--profile media` 只要照片视频（默认）；`docs` 只要文档；`all` 全都要
- `--exclude-live` 用 NTFS 表跳过**还存在**的文件，只挖真正丢失的部分（推荐）
- 中途 Ctrl+C 不会丢进度，续跑加 `--resume`

**第 3 步：按 NTFS 删除记录恢复原名。** 挖照片时这一步最值钱：

```powershell
python -m unearth ntfs vol:E: -o D:\dig\e
```

会打印类似：

```
2013-08-12 19:22      2.31 MiB  DCIM/100CANON/IMG_1234.JPG
2014-02-03 11:05    184.00 KiB  DCIM/100CANON/IMG_1301.JPG
```

**第 4 步：筛选。** 打分、去重、生成相册和报告：

```powershell
python -m unearth triage D:\dig\e
start D:\dig\e\album\index.html
```

相册是纯静态 HTML，离线可开。顶部筛选条：类型 / 年月 / 像素 / 相机 / GPS / 仅完整 / 隐藏重复 / 只看收藏；点缩略图看大图和来源（磁盘偏移、原文件夹、相机、GPS）；`★` 收藏；`导出当前筛选清单` 会下载 `picked.json`。

**第 5 步：把挑中的复制出来。**

```powershell
python -m unearth export D:\dig\e -d "D:\留档\2013" --picks "$env:USERPROFILE\Downloads\picked.json"
```

不挑，全都要（自动去重、按日期分目录）：

```powershell
python -m unearth export D:\dig\e -d D:\留档 --all
```

## 命令

| 命令 | 干什么 |
|---|---|
| `unearth drives` | 列出卷/物理盘，检查能不能读 |
| `unearth scan <目标> -o <输出>` | 签名雕刻：照片/视频/文档成批挖出来 |
| `unearth ntfs <目标> -o <输出>` | 按 NTFS 删除记录恢复（原名、原目录、时间） |
| `unearth triage <输出>` | 打分、去重、生成相册 + 报告 |
| `unearth album <输出>` | 只重建相册 |
| `unearth report <输出>` | 打印 markdown 报告 |
| `unearth export <输出> -d <目标目录>` | 导出（可带 `--picks`） |
| `unearth selftest` | 假镜像全流程自检 |

目标写法：`vol:E:`（分区）、`disk:2`（整块盘）、`img:D:\backup\old.img`（镜像文件）、`\\.\PhysicalDrive2`。

## 输出目录长什么样

```
D:\dig\e\
  catalog.db          SQLite，每个命中一行：偏移、大小、sha256、EXIF、时间、评分
  carved\photo\       jpeg_0000001a2b3c.jpg（文件名带磁盘偏移，方便回溯）
  carved\video\
  carved\doc\
  ntfs\DCIM\IMG_1234.JPG   ← 只有这一步来的文件保留原始名字和目录
  album\index.html    离线相册
  album\index.json    相册数据（给别的工具用）
  report.md           给人看的总结
```

`catalog.db` 是唯一的真相来源：相册、报告、导出都从它生成，重跑 `triage` 不会重新读盘。

## 它能恢复什么、不能恢复什么

**能**：JPEG/PNG/GIF/BMP/TIFF/RAW(CR2/NEF/ARW/DNG)/HEIC/AVIF、MP4/MOV/3GP/M4A/AVI/WAV、PDF/ZIP/DOCX/XLSX/PPTX/DOC/XLS/PPT/MSG。

**不能**（至少现在）：

- **碎片化**的删除文件。雕刻只能把"物理连续"的一段当成一个文件；碎了就只留前半截（工具会标成 `不完整`，相册里虚框）。
- **加密**（BitLocker）或已用安全擦除/TRIM 覆盖的数据。SSD 开了 TRIM 基本没戏；机械盘、U 盘、SD 卡成功率高得多。
- 已格式化成别的文件系统的分区，仍能雕刻出内容，但拿不到原名。
- exFAT/FAT32 的**删除文件名**（第一版只解析 NTFS；exFAT 卡上的删除照片仍能雕刻出来，只是名字丢了）。

## 它凭什么说"这个文件是好文件"

雕刻器不是见到 magic 就截一段，而是真的解析结构：JPEG 走到 EOI，ZIP 读中央目录，MP4 走 box 链到 `moov`/`mdat`，OLE 读 FAT 表算扇区，NTFS 读 runlist。解析不过就当没找到——所以假阳性很少，`完整` 这个字段是有意义的。

打分（0–100）是个可以逐条检查的加法，不是模型：

| 加分项 | 值 |
|---|---|
| 有拍摄时间（EXIF/MP4 创建时间） | +22 |
| 有相机型号 | +14 |
| 像素 ≥ 6MP / ≥ 2MP / ≥ 0.5MP | +14 / +11 / +6 |
| 有 GPS | +8 |
| 文件完整（解析到结尾） | +6 |
| 视频 / 照片 | +18 / +12 |
| 文件名像缩略图/截图/缓存 | −12 |
| 短边 < 240px | −8 |

## 开发

```powershell
python -m unittest discover -s tests -t . -v     # 44 个用例，全部基于合成镜像
python -m unearth selftest                        # 端到端，16 项断言
node tools/check_album.js D:\dig\e\album\index.html   # 无浏览器校验相册渲染（可选，需 Node）
```

结构见 [docs/DESIGN.md](docs/DESIGN.md)，下一步（"挖掘丢失的工作文件"）见 [docs/ROADMAP.md](docs/ROADMAP.md)。

## 已验证 / 待验证

已验证（本机，合成数据）：签名雕刻的 10 种格式、NTFS 记录解析与按 runlist 取数据、去重、评分、相册渲染、导出、自检全流程。

**待你在真盘上验证**：`\\\\.\\PhysicalDriveN` 的真实读取（需要管理员 + 一块旧盘）。第一次跑建议先拿一块不重要的 U 盘/旧移动硬盘试：

```powershell
python -m unearth drives
python -m unearth scan vol:E: -o D:\dig\test --profile media --limit-files 50 -v
```

`--limit-files 50` 命中 50 个就停，用来确认路径通不通，几秒钟的事。

## 许可

MIT
