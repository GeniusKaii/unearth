"""Command line interface - the only file most users need to read."""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__, catalog as catalog_mod, device, probe, util
from .album import build_album
from .catalog import Catalog
from .export import export_selection, load_picks
from .formats.registry import PROFILES
from .fs.ntfs import NotNtfs, Ntfs, extract
from .report import write_report
from .scanner import ScanOptions, scan_source
from .triage import kind_from_ext, run_triage, summarize
from .util import MIB, Progress, human_bytes

EPILOG = """\
典型用法:
  unearth drives                                  看有哪些盘、哪些能读
  unearth scan vol:E: -o D:\\dig\\e                 整个分区雕刻（管理员终端）
  unearth ntfs vol:E: -o D:\\dig\\e                 额外按 NTFS 删除记录恢复原名
  unearth triage D:\\dig\\e                        打分/去重/生成相册
  unearth export D:\\dig\\e -d D:\\留档 --all        导出全部
  unearth selftest                               先用假镜像自检，不碰真盘

铁律: 只读打开源设备；输出永远写到另一块盘。
"""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _fail(msg: str, code: int = 2) -> int:
    print("错误: " + msg, file=sys.stderr)
    return code


def _need_admin_hint(exc: OSError) -> str:
    code = getattr(exc, "errno", 0) or 0
    if code == 5:
        return ("没有权限读取原始设备。请用“以管理员身份运行”的终端重开，"
                "或改用磁盘镜像 (img:路径)。")
    return str(exc)


def _out_volume(out_dir: str) -> str:
    drive, _ = os.path.splitdrive(os.path.abspath(out_dir))
    return drive.upper()


def _source_volume(spec: str) -> str:
    low = spec.lower()
    if low.startswith("vol:"):
        letter = spec.split(":", 1)[1].strip().strip("\\").rstrip(":").upper()
        return (letter[:1] + ":").upper()
    return ""


def _guard_output(out_dir: str, spec: str, force: bool) -> str:
    src_vol = _source_volume(spec)
    if src_vol and _out_volume(out_dir) == src_vol:
        if not force:
            return ("输出目录在源分区本身上 (%s)。写进去会覆盖你要恢复的数据。"
                    "换一个分区，或加 --force（不推荐）。" % src_vol)
    return ""


def _open_catalog(out_dir: str, readonly: bool = False) -> Catalog:
    return Catalog(os.path.join(out_dir, "catalog.db"), readonly=readonly)


# --------------------------------------------------------------------------- #
# drives
# --------------------------------------------------------------------------- #
def cmd_drives(args) -> int:
    print("正在探测磁盘（读取原始设备需要管理员权限）...")
    volumes = probe.list_volumes()
    disks = probe.list_disks()
    text = probe.describe_targets(volumes, disks)
    print(text if text else "没有探测到可读的磁盘。")
    unreadable = [v for v in volumes if not v.readable]
    if unreadable:
        print("")
        print("卷 %s 目前读不到原始数据：%s"
              % (", ".join(v.letter + ":" for v in unreadable[:8]),
                 unreadable[0].error))
        print("用“以管理员身份运行”的 PowerShell 重开，就能读到裸设备了。")
    if not disks:
        print("")
        print("物理磁盘列表需要管理员权限才能枚举（\\\\.\\PhysicalDriveN）。")
        print("普通卷也有意义：unearth scan vol:E: 就能扫整个分区。")
    print("")
    print("下一步: unearth scan vol:<盘符>: -o <另一个盘上的输出目录>")
    return 0


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
def cmd_scan(args) -> int:
    try:
        start = util.parse_size(args.start)
        end = util.parse_size(args.end) if args.end else None
        chunk = util.parse_size(args.chunk)
    except ValueError as exc:
        return _fail(str(exc))

    guard = _guard_output(args.out, args.target, args.force)
    if guard:
        return _fail(guard)

    try:
        src = device.open_source(args.target)
    except OSError as exc:
        return _fail(_need_admin_hint(exc))
    except ValueError as exc:
        return _fail(str(exc))

    util.ensure_dir(args.out)
    cat = _open_catalog(args.out)
    try:
        if start >= src.size:
            return _fail("起始偏移超出设备大小")
        end = min(end or src.size, src.size)
        source_id = cat.add_source(src.spec, src.kind, src.label, src.size, src.sector_size)

        scan_id = None
        if args.resume:
            row = cat.last_open_scan(args.target, args.profile)
            if row:
                scan_id = int(row["id"])
                start = max(start, int(row["cursor"] or 0))
                print("断点续扫：从 %s 继续" % human_bytes(start))
                fixed = cat.reconcile_carved(args.out)
                if fixed["removed_orphan_files"] or fixed["removed_orphan_rows"]:
                    print("崩溃对账：清理盘上孤儿文件 %d 个、库中孤儿行 %d 行"
                          % (fixed["removed_orphan_files"],
                             fixed["removed_orphan_rows"]))
        if scan_id is None:
            scan_id = cat.begin_scan(source_id, args.target, args.profile, start, end, chunk)

        skip_ranges = []
        if args.exclude_live:
            try:
                ntfs = Ntfs(src, base=util.parse_size(args.offset))
                print("读取 NTFS 表以排除仍然存在的文件 ...")
                skip_ranges = ntfs.live_extents()
                print("  活动文件区间 %d 段" % len(skip_ranges))
            except (NotNtfs, OSError) as exc:
                print("  跳过（不是可读的 NTFS：%s）" % exc)

        print("源: %s  %s  %s" % (src.spec, human_bytes(src.size),
                                  "只读" if src.readonly else ""))
        print("范围: %s .. %s   块 %s   模式 %s"
              % (human_bytes(start), human_bytes(end), human_bytes(chunk), args.profile))
        print("输出: %s" % os.path.abspath(args.out))
        print("")

        opts = ScanOptions(
            profile=args.profile, chunk=chunk, start=start, end=end,
            out_dir=args.out, keep_partial=not args.drop_partial,
            min_size=util.parse_size(args.min_size),
            max_size=util.parse_size(args.max_size) if args.max_size != "0" else 0,
            verbose=args.verbose, skip_ranges=skip_ranges,
            stop_after=args.limit_files,
        )
        progress = Progress(end - start, "scan", enabled=sys.stderr.isatty())
        t0 = time.time()
        try:
            stats = scan_source(src, cat, scan_id, opts, progress=progress)
        except KeyboardInterrupt:
            progress.close()
            cat.finish_scan(scan_id, "interrupted", "user interrupt")
            print("\n已中断。续扫: unearth scan %s -o %s --resume"
                  % (args.target, args.out))
            return 130
        progress.close()
        elapsed = max(time.time() - t0, 1e-6)
        if stats.get("stopped_early"):
            cat.finish_scan(scan_id, "stopped_early",
                            "limit_files=%d" % args.limit_files)
        else:
            cat.finish_scan(scan_id, "done")
        print("")
        print("扫描完成  %s  读取 %s  平均 %s"
              % (util.human_duration(elapsed), human_bytes(stats["bytes"]),
                 util.human_rate(stats["bytes"] / elapsed)))
        print("命中 %d 个文件，共 %s" % (stats["files"], human_bytes(stats["size"])))
        if stats["by_kind"]:
            print("  " + "  ".join("%s %d" % (k, v) for k, v in stats["by_kind"].items()))
        if stats["partial"]:
            print("其中 %d 个不完整（尾部被覆盖或索引缺失），相册里会用虚框标出"
                  % stats["partial"])
        if stats["skipped_live"]:
            print("跳过 %d 个仍然存在的文件（--exclude-live）" % stats["skipped_live"])
        skipped = stats.get("skipped_too_big", 0) + stats.get("skipped_too_small", 0) \
            + stats.get("skipped_off_end", 0)
        if skipped:
            print("另有 %d 个命中未入库：超大小上限 %d / 低于下限 %d / 伸出盘尾 %d"
                  % (skipped, stats.get("skipped_too_big", 0),
                     stats.get("skipped_too_small", 0),
                     stats.get("skipped_off_end", 0)))
        if stats.get("stopped_early"):
            print("命中数到达 --limit-files %d，提前停止（scan 表已标记 stopped_early）"
                  % args.limit_files)
        if stats["errors"]:
            print("解析失败 %d 次（已忽略）" % stats["errors"])
        print("")
        print("下一步: unearth triage %s" % args.out)
        return 0
    finally:
        src.close()
        cat.close()


# --------------------------------------------------------------------------- #
# ntfs
# --------------------------------------------------------------------------- #
def cmd_ntfs(args) -> int:
    guard = _guard_output(args.out, args.target, args.force)
    if guard:
        return _fail(guard)
    try:
        src = device.open_source(args.target)
    except OSError as exc:
        return _fail(_need_admin_hint(exc))

    util.ensure_dir(args.out)
    cat = _open_catalog(args.out)
    try:
        base = util.parse_size(args.offset)
        try:
            ntfs = Ntfs(src, base=base)
        except NotNtfs as exc:
            return _fail("这个位置不是 NTFS：%s" % exc)
        print(ntfs.describe())
        source_id = cat.add_source(src.spec, src.kind, src.label, src.size, src.sector_size)
        scan_id = cat.begin_scan(source_id, args.target, "ntfs", 0, src.size, 0)

        out_root = util.ensure_dir(os.path.join(args.out, "ntfs"))
        taken = set()
        found = []
        t0 = time.time()
        limit = args.limit or 0
        for rec in ntfs.deleted_files():
            rel = ntfs.rel_path(rec)
            dest_dir = util.ensure_dir(os.path.join(out_root, rel)) if rel else out_root
            path, written, complete = extract(src, ntfs, rec, dest_dir, taken)
            if not path:
                continue
            sha = util.sha256_file(path)
            ext = rec.ext
            kind = kind_from_ext(ext, "other")
            captured = util.iso_or_empty(rec.modified)
            cat.add_file(
                scan_id=scan_id, src=src.spec, kind=kind, fmt="ntfs", ext=ext,
                off=ntfs.mft_offset + rec.index * ntfs.record_size,
                size=written, complete=int(complete), name=os.path.basename(path),
                path=util.rel_posix(path, args.out), sha256=sha,
                captured=captured, day=util.day_key(rec.modified),
                note="MFT 记录 #%d，修改时间取自文件表" % rec.index,
            )
            found.append((rec.modified, os.path.basename(path), written, complete, rel))
            if limit and len(found) >= limit:
                break
            if len(found) % 200 == 0:
                print("  ... 已恢复 %d 个" % len(found))
        cat.commit()
        status = "done" if not limit else "stopped_early"
        cat.finish_scan(scan_id, status,
                        "limit=%d" % limit if limit else "")

        elapsed = max(time.time() - t0, 1e-6)
        print("")
        print("恢复 %d 个被删除文件，用时 %s" % (len(found), util.human_duration(elapsed)))
        for stamp, name, size, complete, rel in found[:20]:
            print("  %s  %10s  %s%s" % (util.fmt_dt(stamp), human_bytes(size),
                                        (rel + "/") if rel else "", name))
        if len(found) > 20:
            print("  ... 还有 %d 个，全部在 %s" % (len(found) - 20, os.path.abspath(out_root)))
        if not found:
            print("没有找到可恢复的删除记录（可能记录已被清空，或用签名扫描更稳）")
        print("")
        print("下一步: unearth triage %s" % args.out)
        return 0
    finally:
        src.close()
        cat.close()


# --------------------------------------------------------------------------- #
# triage / album / report / export
# --------------------------------------------------------------------------- #
def cmd_triage(args) -> int:
    cat = _open_catalog(args.out)
    try:
        if not cat.count_files():
            return _fail("目录里还没有扫描结果，先跑 scan 或 ntfs")
        progress = Progress(0, "triage", enabled=sys.stderr.isatty())
        t0 = time.time()
        res = run_triage(args.out, cat, progress=progress)
        progress.close()
        path = write_report(args.out, cat)
        stats = summarize(args.out, cat)
        print("打分 %d 个，重复 %d 个，用时 %s"
              % (res["scored"], res["dupes"], util.human_duration(time.time() - t0)))
        print("文件 %d（去重后 %d），共 %s"
              % (stats["files"], stats["unique"], human_bytes(stats["bytes"])))
        for kind, n in stats["by_kind"].items():
            print("  %-8s %d" % (kind, n))
        print("")
        print("相册: %s" % os.path.abspath(res["album"]))
        print("报告: %s" % os.path.abspath(path))
        print("双击相册即可浏览、筛选、收藏，然后导出清单。")
        return 0
    finally:
        cat.close()


def cmd_album(args) -> int:
    cat = _open_catalog(args.out)
    try:
        progress = Progress(len(list(cat.iter_files())), "album", enabled=sys.stderr.isatty())
        path = build_album(args.out, cat, make_thumbs=not args.no_thumbs, progress=progress)
        progress.close()
        print("相册已更新: %s" % os.path.abspath(path))
        return 0
    finally:
        cat.close()


def cmd_report(args) -> int:
    cat = _open_catalog(args.out)
    try:
        path = write_report(args.out, cat)
        print(open(path, encoding="utf-8").read())
        print("报告已写入 %s" % os.path.abspath(path))
        return 0
    finally:
        cat.close()


def cmd_export(args) -> int:
    cat = _open_catalog(args.out)
    try:
        ids = None
        if args.picks:
            ids = load_picks(args.picks)
        progress = Progress(0, "export", enabled=sys.stderr.isatty())
        stats = export_selection(args.out, cat, args.dest, ids=ids,
                                 only_complete=args.only_complete,
                                 flat=args.flat, progress=progress)
        progress.close()
        print("导出 %d 个文件，%s → %s"
              % (stats["copied"], human_bytes(stats["bytes"]), stats["dest"]))
        if stats["skipped_dup"]:
            print("跳过重复 %d 个" % stats["skipped_dup"])
        if stats["skipped_partial"]:
            print("跳过不完整 %d 个" % stats["skipped_partial"])
        if stats["skipped_missing"]:
            print("跳过缺失 %d 个" % stats["skipped_missing"])
        return 0
    finally:
        cat.close()


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #
def cmd_selftest(args) -> int:
    from .selftest import run

    res = run(size=util.parse_size(args.size), keep=args.keep,
              workdir=args.dir or None)
    if args.keep:
        print("")
        print("现场保留在 %s" % res["workdir"])
    return 0 if res["ok"] else 1


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="unearth", description="拾遗 unearth - 从旧硬盘里找回被删除的照片、视频和文档",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version="unearth " + __version__)
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("drives", help="列出磁盘/分区，检查哪些能读")
    sp.set_defaults(func=cmd_drives)

    sp = sub.add_parser("scan", help="签名雕刻：把照片/视频/文档成批挖出来")
    sp.add_argument("target", help="disk:0 / vol:E: / img:路径 / \\\\.\\PhysicalDrive0")
    sp.add_argument("-o", "--out", required=True, help="输出目录（必须在另一块盘上）")
    sp.add_argument("--profile", default="media", choices=sorted(PROFILES),
                    help="media=照片视频(默认) photos videos docs all")
    sp.add_argument("--start", default="0", help="起始偏移，支持 4G 这种写法")
    sp.add_argument("--end", default="", help="结束偏移")
    sp.add_argument("--chunk", default="16M", help="每次读取的块大小")
    sp.add_argument("--min-size", default="0", help="小于此大小的命中丢弃")
    sp.add_argument("--max-size", default="0", help="大于此大小的命中丢弃")
    sp.add_argument("--drop-partial", action="store_true", help="不保留不完整的文件")
    sp.add_argument("--resume", action="store_true", help="从上次中断处继续")
    sp.add_argument("--exclude-live", action="store_true",
                    help="用 NTFS 表跳过仍然存在的文件，只挖真正丢失的")
    sp.add_argument("--offset", default="0", help="分区在整盘里的起始偏移（配 --exclude-live）")
    sp.add_argument("--limit-files", type=int, default=0, help="命中这么多就停（调试用）")
    sp.add_argument("-v", "--verbose", action="store_true", help="每找到一个就打印")
    sp.add_argument("--force", action="store_true", help="允许输出写在源盘上（危险）")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("ntfs", help="按 NTFS 删除记录恢复：保留原始文件名和时间")
    sp.add_argument("target", help="vol:E: / disk:2 / img:路径")
    sp.add_argument("-o", "--out", required=True)
    sp.add_argument("--offset", default="0", help="分区起始偏移（扫整盘镜像时用）")
    sp.add_argument("--limit", type=int, default=0, help="最多恢复多少个")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_ntfs)

    sp = sub.add_parser("triage", help="打分、去重、生成相册和报告")
    sp.add_argument("out", help="scan 的输出目录")
    sp.set_defaults(func=cmd_triage)

    sp = sub.add_parser("album", help="只重建相册")
    sp.add_argument("out")
    sp.add_argument("--no-thumbs", action="store_true", help="不生成缩略图（更快）")
    sp.set_defaults(func=cmd_album)

    sp = sub.add_parser("report", help="打印/刷新 markdown 报告")
    sp.add_argument("out")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("export", help="把（筛选后的）文件复制到目标目录")
    sp.add_argument("out")
    sp.add_argument("-d", "--dest", required=True)
    sp.add_argument("--picks", help="相册导出的 picked.json")
    sp.add_argument("--all", action="store_true", help="导出全部（默认行为）")
    sp.add_argument("--only-complete", action="store_true", help="只导完整文件")
    sp.add_argument("--flat", action="store_true", help="不按日期建子目录")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("selftest", help="用假镜像全流程自检（不碰真盘）")
    sp.add_argument("--size", default="24M")
    sp.add_argument("--dir", default="", help="自检工作目录（保留证据用）")
    sp.add_argument("--keep", action="store_true", help="保留临时文件")
    sp.set_defaults(func=cmd_selftest)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except (OSError, ValueError) as exc:
        return _fail(str(exc))
