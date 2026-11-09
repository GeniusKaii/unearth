"""End to end self test on synthetic data: no disk, no admin, no dependencies.

This is the thing to run first on a new machine.  It writes a fake "disk" with
known deleted photos, a fake NTFS volume with known deleted records, carves
both, triages, builds the album and exports a selection - then checks every
step.  If this passes, the tool works; what is left is your actual drive.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile

from . import fixtures, util
from .catalog import Catalog
from .device import FileSource
from .export import export_selection
from .fs.ntfs import Ntfs
from .report import write_report
from .scanner import ScanOptions, scan_source
from .triage import run_triage

MIB = 1024 * 1024


def _sha(path: str) -> str:
    return util.sha256_file(path, 8 * MIB)


def run(workdir: str | None = None, size: int = 24 * MIB, keep: bool = False,
        out=print) -> dict:
    checks = []

    def check(name: str, ok, detail: str = "") -> bool:
        checks.append((name, bool(ok), detail))
        out("  %s %-46s %s" % ("[ok]  " if ok else "[FAIL]", name, detail))
        return bool(ok)

    tmp = workdir or tempfile.mkdtemp(prefix="unearth-selftest-")
    os.makedirs(tmp, exist_ok=True)
    try:
        os.chmod(tmp, 0o777)   # mkdtemp's 0700 ACL trips some Windows sandboxes
    except OSError:
        pass
    ok_all = True
    try:
        out("unearth selftest  (临时目录 %s)" % tmp)
        out("")
        out("1. 造一个假硬盘镜像")
        image_path = os.path.join(tmp, "demo.img")
        with open(image_path, "wb") as fh:
            fh.write(fixtures.build_demo_image(size))
        before = _sha(image_path)
        out("   %s  %s" % (image_path, util.human_bytes(os.path.getsize(image_path))))

        out("")
        out("2. 签名雕刻扫描")
        out_dir = os.path.join(tmp, "dig")
        cat = Catalog(os.path.join(out_dir, "catalog.db"))
        src = FileSource(image_path)
        source_id = cat.add_source(src.spec, "image", src.label, src.size, src.sector_size)
        scan_id = cat.begin_scan(source_id, src.spec, "all", 0, src.size, 8 * MIB)
        opts = ScanOptions(profile="all", chunk=8 * MIB, out_dir=out_dir)
        stats = scan_source(src, cat, scan_id, opts)
        cat.finish_scan(scan_id, "done")
        src.close()
        cat.commit()

        ok_all &= check("扫描命中 ≥ 12 个文件", stats["files"] >= 12,
                        "实际 %d 个" % stats["files"])
        for fmt in ("jpeg", "isobmff", "zip", "pdf", "ole"):
            ok_all &= check("找到 %s" % fmt, stats["by_fmt"].get(fmt, 0) > 0,
                            "%d 个" % stats["by_fmt"].get(fmt, 0))
        row = cat.conn.execute(
            "SELECT * FROM file WHERE fmt='jpeg' AND captured<>'' LIMIT 1").fetchone()
        ok_all &= check("JPEG 抓到拍摄时间", row is not None,
                        row["captured"] if row else "没抓到 EXIF")
        row = cat.conn.execute(
            "SELECT * FROM file WHERE fmt='isobmff' LIMIT 1").fetchone()
        ok_all &= check("视频帧尺寸/时长", row is not None and row["width"] and row["duration"],
                        "%sx%s %.1fs" % (row["width"], row["height"], row["duration"]) if row else "")
        after = _sha(image_path)
        ok_all &= check("源镜像字节未改变（只读铁律）", before == after)

        out("")
        out("3. NTFS 删除记录恢复（原始文件名 + 时间）")
        ntfs_path = os.path.join(tmp, "ntfs.img")
        with open(ntfs_path, "wb") as fh:
            fh.write(fixtures.make_ntfs_image())
        nsrc = FileSource(ntfs_path)
        ntfs = Ntfs(nsrc)
        out("   " + ntfs.describe())
        names = []
        ntfs_dir = os.path.join(out_dir, "ntfs")
        util.ensure_dir(ntfs_dir)
        from .fs.ntfs import extract

        for rec in ntfs.deleted_files():
            rel = ntfs.rel_path(rec)
            dest_dir = util.ensure_dir(os.path.join(ntfs_dir, rel)) if rel else ntfs_dir
            path, written, complete = extract(nsrc, ntfs, rec, dest_dir)
            if path:
                names.append(os.path.basename(path))
        ok_all &= check("找回被删除的文件名", "IMG_1234.JPG" in names, str(names))
        recovered = os.path.join(ntfs_dir, "DCIM", "IMG_1234.JPG")
        ok_all &= check("恢复出的 JPEG 与原始字节一致",
                        os.path.exists(recovered) and
                        open(recovered, "rb").read() == fixtures.jpeg_with_exif())
        nsrc.close()

        out("")
        out("4. 打分 / 去重 / 生成相册")
        res = run_triage(out_dir, cat)
        ok_all &= check("相册 index.html 已生成", os.path.exists(res["album"]))
        import json

        with open(os.path.join(out_dir, "album", "index.json"), encoding="utf-8") as fh:
            payload = json.load(fh)
        ok_all &= check("相册索引可解析且有内容", len(payload.get("items", [])) > 0,
                        "%d 条" % len(payload.get("items", [])))
        scored = cat.conn.execute("SELECT COUNT(*) FROM file WHERE score>0").fetchone()[0]
        ok_all &= check("文件已打分", scored > 0, "%d 个" % scored)

        out("")
        out("5. 报告 + 导出")
        report_path = write_report(out_dir, cat)
        ok_all &= check("report.md 已生成", os.path.exists(report_path))
        dest = os.path.join(tmp, "exported")
        exp = export_selection(out_dir, cat, dest)
        ok_all &= check("导出成功", exp["copied"] > 0,
                        "%d 个 / %s" % (exp["copied"], util.human_bytes(exp["bytes"])))
        cat.close()
    finally:
        if not keep and workdir is None:
            shutil.rmtree(tmp, ignore_errors=True)

    failed = [c for c in checks if not c[1]]
    out("")
    if failed:
        out("结果：%d/%d 通过，%d 项失败" % (len(checks) - len(failed), len(checks), len(failed)))
    else:
        out("结果：%d/%d 全部通过。工具可用。" % (len(checks), len(checks)))
    return {"ok": not failed, "checks": checks, "workdir": tmp, "stats": stats}
