# 设计说明

## 目标

输入一块旧盘（或镜像），输出**能翻的时间线**，而不是一堆匿名文件。所以整条链路分成两半，中间用一个 SQLite 库隔开：

```
读设备 → 认文件 → 落盘 + 入库 → 打分/去重 → 相册
   ↑ 可以重复、可续跑        ↑ 不碰盘，随便重跑
```

这条切分很重要：扫描慢（受 IO 限制），筛选快（纯 CPU）。用户可能扫三小时、挑十天，中间反复生成相册，所以**任何筛选逻辑都不能依赖重新读盘**。

## 不变量

1. `device.py` 里没有任何写句柄；`Source` 类不提供写方法。
2. 输出目录落在源分区时，`cli._guard_output()` 直接拒绝（除非 `--force`）。
3. 每个恢复文件都记录 `(off, size, sha256)`，任何结论都能回到磁盘位置。

## 模块

| 模块 | 职责 |
|---|---|
| `device.py` | 只读打开 `\\\\.\\PhysicalDriveN` / `\\\\.\\X:` / 镜像文件；`ctypes` + `ReadFile`，按扇区对齐 |
| `probe.py` | 枚举卷/分区；不需要管理员也能拿到卷标、文件系统、容量（`GetVolumeInformationW` + `statvfs`） |
| `readers.py` | `Reader`：4 MiB 前向窗口缓存，解析器逐字段读也不炸 syscall；`materialize()` 分块抄盘 + 流式 sha256 |
| `formats/*` | 每种格式一个"魔数 + 测量器"，返回 `(size, complete, meta)` |
| `scanner.py` | 分块扫描引擎：收集块内所有魔数命中，**按偏移排序**后依次雕刻 |
| `fs/ntfs.py` | 解析 `$MFT`：`FILE` 记录、fixup、`$FILE_NAME`、`$STANDARD_INFORMATION`、`$DATA` runlist |
| `catalog.py` | SQLite 表结构；`file` 表一行一个命中 |
| `triage/` | 评分、精确去重（sha256）、重复组的"保留哪个" |
| `album.py` | 生成静态相册（内嵌 JSON，不依赖 fetch，`file://` 直接能开） |
| `export.py` / `report.py` | 复制挑选结果 / 生成 markdown 总结 |
| `fixtures.py` | 合成 EXIF、DOCX、PDF、OLE、NTFS 卷、假硬盘镜像 |
| `selftest.py` | 用合成数据跑完整链路，16 项断言 |

## 为什么是"雕刻 + MFT"两条腿

- **MFT（NTFS）** 给出原名、原目录、四个时间戳、真实大小和簇链——信息质量最高，但只在 NTFS 且记录没被覆盖时可用。
- **签名雕刻** 不依赖文件系统，什么盘都能挖，代价是只有内容没有名字。

两条腿互补：先用 MFT 拿"有名字的那部分"，再用雕刻覆盖"MFT 已经被覆盖的那部分"。同一个文件两边都命中时，sha256 去重会合并。

## 扫描引擎的几个决定

**按偏移排序后雕刻。** 最初的实现是按格式循环（先所有 JPEG，再所有 MP4）。结果是 MP4 里的封面 JPEG 先被雕刻走，占住了 `[off, off+size)`，随后 MP4 自己因为"起始偏移落在已占领区间内"被跳过——测试里 12 个 MP4 只找到 2 个。改成把一块内所有命中收齐、按偏移从小到大雕刻，外层容器先落地，内层自然被吞掉。这也是 `claimed_end` 单调递增的前提。

**块间重叠只用来发现跨界的魔数。** `abs_off >= off + n` 的命中交给下一块，避免重复；额外往前读 8 字节，避免 4 字节魔数正好压在块边界上时漏掉。

**测不出大小就不算命中。** 每个 carver 都要给出确切长度：TIFF 用 strip 几何算、ZIP 走中央目录、OLE 读 FAT 表、MP4 走 box 链。宁可漏，不可假——`complete` 字段是给用户看的承诺。

**断点续扫。** 每 5 秒或 256 MiB 记一次 `scan.cursor`；`--resume` 从 cursor 继续。Ctrl+C 也写 cursor。

## 相册为什么是单文件静态 HTML

- `fetch('index.json')` 在 `file://` 下被 CORS 拦掉，所以数据直接内嵌成 `<script type="application/json">`（`<` 转义成 `\u003c`，防止文件名里出现 `</script>`）。
- 没有服务器、没有网络、没有依赖，拷到 U 盘、发给别人都能开。
- 20 万条以内浏览器扛得住；超过会把前 20 万条嵌入并在 `report.md` 里提示。

## 已知取舍

- **不做文件系统级"未分配空间"图**。整盘扫描会把仍然存在的文件也雕刻一遍，所以提供 `--exclude-live`（用 MFT 的活动簇区间跳过）。默认关闭，因为不是所有盘都有可读 MFT。
- **不做碎片拼接**。JPEG 的 restart marker / MP4 的 sample table 理论上能拼，第一版不做，只标 `不完整`。
- **不碰加密**。BitLocker 卷在解锁状态下（`vol:E:` 挂载后）其实是可读的——那是 Windows 解密后的视图，这点在 README 里没说清，算已知空白。
- **单进程**。雕刻阶段 IO 密集，多进程收益有限；真要并行，可以按 `--start/--end` 手动切几段跑。

## 数据结构（`catalog.db`）

```sql
source(id, spec, kind, label, size, sector, note, created)
scan(id, source_id, target, profile, range_start, range_end, chunk,
     cursor, started, finished, status, bytes, files, note)
file(id, scan_id, src, kind, fmt, ext, off, size, complete, name, path,
     sha256, width, height, duration, captured, day, make, model,
     gps_lat, gps_lon, orientation, score, dup_group, thumb, note, created)
tag(file_id, tag)
```

`off` 是**设备绝对偏移**，`path` 是相对输出目录的路径。这两列合起来，任何一个恢复文件都能回到它在盘上的原始位置。
