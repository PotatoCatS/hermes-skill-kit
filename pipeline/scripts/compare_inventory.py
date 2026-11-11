#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_inventory.py — 对比两次盘点结果（SKM 阶段 2 交付物 2/2）

检测：
  * 新增 Skill（新 Registry 出现、旧 Registry 没有）
  * 删除 Skill（旧 Registry 有、新 Registry 没有）
  * 内容变化（同一路径清单哈希不同，含逐文件差异）
  * 路径变化（同名 Skill 出现在不同路径，移动候选）
  * 当前扫描异常（新 Registry 的 errors）

退出码：0 = 无变化；1 = 有变化或对比失败。

用法
----
  python3 compare_inventory.py --old OLD_skills.json --new NEW_skills.json
                               [--out-dir DIR] [--scan-log PATH]
"""

import argparse
import datetime
import json
import os
import sys
import tempfile


def utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_registry(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_atomic(path, text):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".skm-tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def diff_skills(old_skills, new_skills):
    old_paths = set(old_skills)
    new_paths = set(new_skills)

    added = sorted(new_paths - old_paths)
    removed = sorted(old_paths - new_paths)

    changed = []
    for p in sorted(old_paths & new_paths):
        o, n = old_skills[p], new_skills[p]
        if o.get("sha256") != n.get("sha256"):
            ofiles = {f["path"]: f["sha256"] for f in o.get("files", [])}
            nfiles = {f["path"]: f["sha256"] for f in n.get("files", [])}
            file_added = sorted(set(nfiles) - set(ofiles))
            file_removed = sorted(set(ofiles) - set(nfiles))
            file_modified = sorted(
                k for k in (set(ofiles) & set(nfiles)) if ofiles[k] != nfiles[k])
            changed.append({
                "path": p,
                "name": n.get("name") or o.get("name"),
                "old_sha256": o.get("sha256"),
                "new_sha256": n.get("sha256"),
                "files_added": file_added,
                "files_removed": file_removed,
                "files_modified": file_modified,
            })

    # 路径变化：同名技能旧路径消失、新路径出现
    moved = []
    matched_new = set()
    for p in removed:
        old_name = (old_skills[p].get("name") if p in old_skills else None)
        for np in added:
            if np in matched_new:
                continue
            if new_skills[np].get("name") == old_name and old_name is not None:
                moved.append({
                    "name": old_name,
                    "old_path": p,
                    "new_path": np,
                    "content_identical": (
                        old_skills[p].get("sha256") == new_skills[np].get("sha256")),
                })
                matched_new.add(np)
                break
    added_left = [p for p in added if p not in matched_new]
    removed_left = [p for p in removed if not any(m["old_path"] == p for m in moved)]
    return added_left, removed_left, changed, moved


def render_report(args, added, removed, changed, moved, errors, name_collisions):
    lines = []
    lines.append("# 盘点对比报告")
    lines.append("")
    lines.append("- 对比时间：%s" % utcnow_iso())
    lines.append("- 旧 Registry：%s" % args.old)
    lines.append("- 新 Registry：%s" % args.new)
    lines.append("")
    total_changes = len(added) + len(removed) + len(changed) + len(moved)
    lines.append("**结论：%s**" % ("有变化" if total_changes or errors else "无变化"))
    lines.append("")
    for title, items in [
        ("新增 Skill", added),
        ("删除 Skill", removed),
    ]:
        lines.append("## %s（%d）" % (title, len(items)))
        lines.append("")
        if items:
            for it in items:
                lines.append("- %s" % it)
        else:
            lines.append("（无）")
        lines.append("")
    lines.append("## 内容变化（%d）" % len(changed))
    lines.append("")
    if changed:
        for c in changed:
            lines.append("- `%s`（%s）" % (c["path"], c["name"]))
            lines.append("  - 清单哈希：%s → %s" % (c["old_sha256"], c["new_sha256"]))
            if c["files_added"]:
                lines.append("  - 新增文件：%s" % "；".join(c["files_added"]))
            if c["files_removed"]:
                lines.append("  - 删除文件：%s" % "；".join(c["files_removed"]))
            if c["files_modified"]:
                lines.append("  - 修改文件：%s" % "；".join(c["files_modified"]))
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("## 路径变化 / 移动候选（%d）" % len(moved))
    lines.append("")
    if moved:
        for m in moved:
            lines.append("- `%s`：%s → %s（内容%s）" % (
                m["name"], m["old_path"], m["new_path"],
                "一致" if m["content_identical"] else "不同"))
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("## 当前扫描异常（%d）" % len(errors))
    lines.append("")
    if errors:
        for e in errors:
            lines.append("- %s：%s" % (e.get("path"), e.get("error")))
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("## 重名登记")
    lines.append("")
    if name_collisions:
        for c in name_collisions:
            lines.append("- `%s`：%s" % (c["name"], "；".join(c["paths"])))
    else:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="对比两次 SKM 盘点结果")
    ap.add_argument("--old", required=True, help="旧 skills.json")
    ap.add_argument("--new", required=True, help="新 skills.json")
    ap.add_argument("--out-dir", default=None, help="报告输出目录（默认与 --new 同目录）")
    ap.add_argument("--scan-log", default=None, help="扫描日志路径（可选）")
    args = ap.parse_args(argv)

    try:
        old_reg = load_registry(args.old)
    except (OSError, ValueError) as e:
        print("无法读取旧 Registry：%s" % e, file=sys.stderr)
        return 1
    try:
        new_reg = load_registry(args.new)
    except (OSError, ValueError) as e:
        print("无法读取新 Registry：%s" % e, file=sys.stderr)
        return 1

    added, removed, changed, moved = diff_skills(
        old_reg.get("skills", {}), new_reg.get("skills", {}))
    errors = new_reg.get("errors", [])
    collisions = new_reg.get("name_collisions", [])

    report = render_report(args, added, removed, changed, moved, errors, collisions)
    print(report)

    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.new))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_path = os.path.join(out_dir, "compare-report-%s.md" % stamp)
    write_atomic(report_path, report + "\n")

    if args.scan_log:
        line = "- [compare] %s 对比完成：新增 %d / 删除 %d / 变化 %d / 移动 %d / 异常 %d（报告 %s）\n" % (
            utcnow_iso(), len(added), len(removed), len(changed), len(moved),
            len(errors), os.path.basename(report_path))
        try:
            os.makedirs(os.path.dirname(args.scan_log), exist_ok=True)
            with open(args.scan_log, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            print("扫描日志写入失败：%s" % e, file=sys.stderr)

    print("对比报告：%s" % report_path)
    has_changes = bool(added or removed or changed or moved or errors)
    return 1 if has_changes else 0


if __name__ == "__main__":
    sys.exit(main())
