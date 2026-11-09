"""A short markdown report of what the dig found - the thing you can hand over."""

from __future__ import annotations

import os

from . import util
from .triage import dup_primaries, summarize


def _table(rows, header):
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def _source_label(cat) -> str:
    row = cat.conn.execute("SELECT target FROM scan ORDER BY id DESC LIMIT 1").fetchone()
    return row["target"] if row else ""


def build_report(out_dir: str, cat) -> str:
    stats = summarize(out_dir, cat)
    conn = cat.conn
    lines = []
    lines.append("# 拾遗 unearth 报告")
    lines.append("")
    lines.append("- 输出目录：`%s`" % os.path.abspath(out_dir))
    lines.append("- 来源：`%s`" % _source_label(cat))
    lines.append("- 生成时间：%s" % util.now_iso())
    lines.append("- 文件总数：%d（去重后 %d）" % (stats["files"], stats["unique"]))
    lines.append("- 总大小：%s" % util.human_bytes(stats["bytes"]))
    lines.append("- 完整文件：%d，可判读时间：%d，有 GPS：%d"
                 % (stats["complete"], stats["with_time"], stats["with_gps"]))
    lines.append("")

    lines.append("## 按类型")
    kinds = stats["by_kind"]
    if kinds:
        lines.append(_table([(k, n) for k, n in kinds.items()], ("类型", "数量")))
    lines.append("")

    lines.append("## 按格式")
    fmts = list(stats["by_fmt"].items())[:20]
    if fmts:
        lines.append(_table(fmts, ("格式", "数量")))
    lines.append("")

    days = conn.execute(
        "SELECT day, COUNT(*) n, SUM(size) sz FROM file WHERE day<>'' "
        "GROUP BY day ORDER BY day").fetchall()
    if days:
        lines.append("## 时间线（有拍摄/修改时间的文件）")
        per_year = {}
        for row in days:
            per_year.setdefault(row["day"][:4], [0, 0])
            per_year[row["day"][:4]][0] += row["n"]
            per_year[row["day"][:4]][1] += row["sz"] or 0
        lines.append(_table(
            [(y, "%d 个" % v[0], util.human_bytes(v[1])) for y, v in sorted(per_year.items())],
            ("年份", "文件数", "大小")))
        lines.append("")
        busiest = sorted(days, key=lambda r: -r["n"])[:15]
        lines.append("最热闹的日子：")
        lines.append(_table([(r["day"], r["n"], util.human_bytes(r["sz"] or 0))
                             for r in busiest], ("日期", "文件数", "大小")))
        lines.append("")

    cams = conn.execute(
        "SELECT COALESCE(NULLIF(model,''), make) cam, COUNT(*) n FROM file "
        "WHERE COALESCE(make,model,'')<>'' GROUP BY cam ORDER BY n DESC LIMIT 15").fetchall()
    if cams:
        lines.append("## 出现过的设备")
        lines.append(_table([(r["cam"], r["n"]) for r in cams], ("设备", "文件数")))
        lines.append("")

    lines.append("## 怎么用")
    lines.append("")
    lines.append("1. 打开 `album/index.html`（双击即可，纯静态，不联网）。")
    lines.append("2. 用筛选条按年月 / 类型 / 像素 / 相机 / GPS 缩小范围，`★` 收藏想留的。")
    lines.append("3. 点 `导出当前筛选清单` 得到 `picked.json`，然后：")
    lines.append("")
    lines.append("```")
    lines.append("unearth export %s -d D:\\我找回的照片 --picks picked.json" % out_dir)
    lines.append("```")
    lines.append("")
    lines.append("不挑选的话，`unearth export %s -d <目标目录> --all` 导出全部去重后的文件。"
                 % out_dir)
    lines.append("")
    partial = conn.execute(
        "SELECT COUNT(*) FROM file WHERE complete=0").fetchone()[0]
    if partial:
        lines.append("> 注意：%d 个文件标记为不完整（文件尾被覆盖或索引缺失），"
                     "相册里用虚框标出。" % partial)
        lines.append("")
    return "\n".join(lines)


def write_report(out_dir: str, cat) -> str:
    text = build_report(out_dir, cat)
    path = os.path.join(out_dir, "report.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path
