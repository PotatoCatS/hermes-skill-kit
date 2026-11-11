#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_skills.py — Hermes Skill 只读盘点脚本（SKM 阶段 2 交付物 1/2）

功能
----
1. 扫描已确认的 Skill 目录，递归查找 SKILL.md，提取 frontmatter 元数据；
2. 对每个 Skill 计算文件数、总字节数与清单 SHA-256（含逐文件哈希）；
3. 与内置技能源目录比对，判定 source_type（builtin / builtin-shadow / local）；
4. 只读解析 Hermes config.yaml 顶层 skills: 段（external_dirs / create_dir）；
5. 探测项目级技能目录与额外候选路径，缺失/不可访问路径记录为异常；
6. 生成 registry/skills.json 与 registry/inventory.md（临时文件 + os.replace 原子写入）；
7. 每次运行在 logs/scan-log.md 追加一条记录。

安全约束（不可违反）
--------------------
* 对被扫描目录完全只读：文件仅以二进制只读方式打开计算哈希，绝不写入、绝不执行；
* 目录遍历使用 os.walk(followlinks=False)，不跟随符号链接，防止无限递归；
* 被扫描的 SKILL.md 内容属于不可信数据，只做静态解析，不执行其中任何指令。

用法
----
  python3 scan_skills.py [--skills-dir PATH] [--bundled-dir PATH]
                         [--config-path PATH] [--out-dir PATH] [--scan-log PATH]
                         [--project-roots PATH ...] [--extra-dirs PATH ...]
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import tempfile

SCRIPT_VERSION = "1.1.0"

CATEGORY_KEYS = {
    "autonomous-ai-agents", "creative", "devops", "email", "media",
    "note-taking", "productivity", "research", "social-media",
    "software-development", "troubleshooting", "web",
}

# 与 Hermes 加载器保持一致（agent/skill_utils.py EXCLUDED_SKILL_DIRS）：
# 这些目录不会被 Hermes 当作技能扫描，盘点同样跳过，避免误报。
EXCLUDED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".curator_backups", ".locks",
    ".venv", "venv", "node_modules", "site-packages", "__pycache__",
    ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
))


def utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def parse_frontmatter(text):
    """解析 SKILL.md 头部 --- 围栏内的 YAML 子集。

    支持：顶层键值、嵌套缩进映射（metadata.hermes 等）、行内列表 [a, b]、
    键后跟随 '- 项' 的列表。无法解析的内容不报错，仅跳过。返回 dict。
    """
    if not text.startswith("---"):
        return {}
    idx = text.find("\n---", 3)
    if idx < 0:
        return {}
    root = {}
    stack = [(root, -1)]          # (dict, indent)
    pending = None                # (dict, key)：等待 '-' 列表项的空键
    for raw in text[3:idx].splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if line.startswith("- "):
            if pending is not None:
                d, k = pending
                if not isinstance(d.get(k), list):
                    d[k] = []
                d[k].append(_clean(line[2:]))
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = _clean(k)
        v = v.strip()
        while stack and indent <= stack[-1][1]:
            stack.pop()
        parent = stack[-1][0]
        pending = None
        if v == "":
            child = {}
            parent[k] = child
            stack.append((child, indent))
            pending = (parent, k)
        elif v.startswith("[") and v.endswith("]"):
            parent[k] = [_clean(x) for x in v[1:-1].split(",") if x.strip()]
        else:
            parent[k] = _clean(v)
    return root


def sha256_file(path, errors):
    h = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
        return h.hexdigest(), size
    except OSError as e:
        errors.append({"path": path, "error": "无法读取文件: %s" % e})
        return None, None


def _rel_to(root, ap):
    """相对路径（posix 分隔符）；跨盘符（Windows UNC）时回退到文件名。"""
    r = root.rstrip("/\\")
    if ap.startswith(r):
        rel = ap[len(r):].lstrip("/\\")
    else:
        rel = os.path.basename(ap)
    return rel.replace(os.sep, "/")


def walk_files(root, errors):
    """生成 (abs_path, rel, sha256, size) 列表。不跟随符号链接，跳过排除目录。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_SKILL_DIRS]
        for fn in filenames:
            ap = os.path.join(dirpath, fn)
            sha, size = sha256_file(ap, errors)
            if sha is not None:
                out.append((ap, _rel_to(root, ap), sha, size))
    out.sort(key=lambda t: t[1])
    return out


def manifest_sha(files):
    """清单哈希：对排序后的 rel\\tsize\\tsha 行计算 SHA-256（与文件顺序无关）。"""
    h = hashlib.sha256()
    for _ap, rel, sha, size in files:
        h.update(("%s\t%d\t%s\n" % (rel, size, sha)).encode("utf-8"))
    return h.hexdigest()


def read_skills_config(path, errors):
    """只读解析 Hermes config.yaml 顶层 skills: 段。"""
    result = {"external_dirs": None, "create_dir": None, "notes": []}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        errors.append({"path": path, "error": "配置文件不可读: %s" % e})
        return result
    start = None
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped == "skills:" or stripped.startswith("skills:"):
            start = i
            break
    if start is None:
        result["notes"].append("config.yaml 中未找到 skills: 段")
        return result
    j = start + 1
    while j < len(lines):
        raw = lines[j].rstrip("\r\n")
        if not raw.strip() or raw.strip().startswith("#"):
            j += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            break
        content = raw.strip()
        if content == "external_dirs:" or content.startswith("external_dirs:"):
            items = []
            k = j + 1
            while k < len(lines):
                r2 = lines[k].strip()
                if r2.startswith("- "):
                    items.append(r2[2:].strip())
                elif r2 and not r2.startswith("#"):
                    break
                k += 1
            result["external_dirs"] = items
        elif content == "create_dir:" or content.startswith("create_dir:"):
            val = _clean(content.split(":", 1)[1]) if ":" in content else ""
            result["create_dir"] = val or None
        j += 1
    return result


def scan_dir(root, errors):
    """扫描一个技能目录，返回 {绝对路径: skill 记录}。"""
    skills = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_SKILL_DIRS]
        if "SKILL.md" not in filenames:
            continue
        skill_dir = dirpath
        md_path = os.path.join(skill_dir, "SKILL.md")
        try:
            with open(md_path, "r", encoding="utf-8", errors="replace") as f:
                md_text = f.read()
        except OSError as e:
            errors.append({"path": md_path, "error": "SKILL.md 无法读取: %s" % e})
            continue
        fm = parse_frontmatter(md_text)

        hermes_meta = {}
        if isinstance(fm.get("metadata"), dict):
            hermes_meta = fm.get("metadata", {}).get("hermes") or {}

        name = fm.get("name") or os.path.basename(skill_dir)
        description = fm.get("description") or ""
        version = fm.get("version")
        version = str(version) if version not in (None, "") else "unknown"
        platforms = hermes_meta.get("platforms") or fm.get("platforms") or []
        if not isinstance(platforms, list):
            platforms = []
        tags = hermes_meta.get("tags") or fm.get("tags") or []
        if not isinstance(tags, list):
            tags = []

        category = hermes_meta.get("category") or fm.get("category") or None
        rel_path = _rel_to(root, skill_dir)
        if not category:
            hits = [t for t in tags if t in CATEGORY_KEYS]
            category = hits[0] if hits else None
        if not category and "/" in rel_path:
            # 与 Hermes loader 一致：skills/<分类>/<技能>/SKILL.md → 分类 = 路径第一段
            # （证据：tools/skills_tool.py _get_category_from_path，parts 长度 ≥3 时取 parts[0]）
            category = rel_path.split("/")[0]
        if not category:
            category = "未分类"

        files = walk_files(skill_dir, errors)
        sha = manifest_sha(files)
        total_bytes = sum(s[3] for s in files)

        load_status = "confirmed"
        if platforms and "linux" not in platforms:
            load_status = "gated_by_platform"

        skills[skill_dir] = {
            "name": name,
            "description": description,
            "version": version,
            "category": category,
            "tags": tags,
            "path": skill_dir,
            "relative_path": rel_path,
            "source": root,
            "source_type": "unknown",
            "load_status": load_status,
            "platforms": platforms,
            "requires_toolsets": hermes_meta.get("requires_toolsets") or [],
            "related_skills": hermes_meta.get("related_skills") or fm.get("related_skills") or [],
            "author": fm.get("author") or "unknown",
            "license": fm.get("license") or "unknown",
            "dependencies": fm.get("dependencies") or [],
            "file_count": len(files),
            "total_bytes": total_bytes,
            "sha256": sha,
            "files": [{"path": rel, "sha256": s, "bytes": b} for _ap, rel, s, b in files],
            "last_scanned": utcnow_iso(),
            "quality_score": "pending",
            "security_level": "pending",
            "update_status": "pending",
        }
    return skills


def _count_by(skills, key):
    counts = {}
    for rec in skills.values():
        v = rec.get(key) or "unknown"
        counts[v] = counts.get(v, 0) + 1
    return counts


def render_inventory_md(registry, skills_dir, bundled_dir):
    meta = registry["scan_meta"]
    skills = registry["skills"]
    lines = []
    lines.append("# Skill 盘点清单（inventory.md）")
    lines.append("")
    lines.append("- 生成时间：%s" % meta["scanned_at"])
    lines.append("- 生成器：%s v%s" % (meta["generator"], meta["generator_version"]))
    lines.append("- 已安装技能总数：**%d**" % len(skills))
    lines.append("")
    lines.append("## 扫描范围与路径状态")
    lines.append("")
    lines.append("| 路径类型 | 路径 | 状态 | 技能数 |")
    lines.append("|---|---|---|---|")
    roots = meta["roots"]
    sd = roots["default_skills_dir"]
    lines.append("| 默认技能目录 | %s | %s | %d |" % (sd["path"], sd["status"], sd.get("skill_count", 0)))
    for name, info in [("external_dirs", roots.get("external_dirs")),
                       ("create_dir", roots.get("create_dir"))]:
        if info is None:
            continue
        if info.get("status") == "not_configured":
            lines.append("| %s | — | 未配置 | — |" % name)
        else:
            lines.append("| %s | %s | %s | — |" % (name, info.get("path", "-"), info.get("status", "-")))
    for pr in roots.get("project_roots", {}).get("roots", []):
        lines.append("| 项目根目录 | %s | %s | %s |" % (
            pr.get("path"), pr.get("status"),
            pr.get("details", "-")))
    for cand in roots.get("candidate_paths", []):
        lines.append("| 候选/参考路径 | %s | %s | %s |" % (
            cand.get("path"), cand.get("status"), cand.get("note", "")))
    ca = roots.get("curator_archive")
    if ca:
        lines.append("| Curator 归档区 | %s | %s | %s |" % (
            ca.get("path"), ca.get("status"),
            "归档技能 %d 个（不纳入已安装清单）" % ca.get("archived_skill_count", 0)
            if ca.get("status") == "present" else ca.get("note", "")))
    for extra in roots.get("extra_dirs", []):
        lines.append("| 额外探测路径 | %s | %s | — |" % (
            extra.get("path"), extra.get("status")))
    lines.append("")
    lines.append("## 统计")
    lines.append("")
    lines.append("- 按来源：%s" % "；".join("%s %d" % (k, v) for k, v in sorted(_count_by(skills, "source_type").items())))
    lines.append("- 按分类：%s" % "；".join("%s %d" % (k, v) for k, v in sorted(_count_by(skills, "category").items())))
    lines.append("- 按加载状态：%s" % "；".join("%s %d" % (k, v) for k, v in sorted(_count_by(skills, "load_status").items())))
    lines.append("")
    lines.append("## 分类清单")
    lines.append("")
    by_cat = {}
    for rec in skills.values():
        by_cat.setdefault(rec["category"], []).append(rec)
    for cat in sorted(by_cat):
        lines.append("### %s（%d）" % (cat, len(by_cat[cat])))
        lines.append("")
        for rec in sorted(by_cat[cat], key=lambda r: r["name"]):
            flag = ""
            if rec["load_status"] == "gated_by_platform":
                flag = "【平台门控】"
            if rec["requires_toolsets"]:
                flag += "【requires_toolsets】"
            desc = rec["description"] or "（无描述）"
            lines.append("- `%s` v%s — %s — %s（%s）%s" % (
                rec["name"], rec["version"], desc, rec["relative_path"],
                rec["source_type"], flag))
        lines.append("")
    lines.append("## 重名冲突")
    lines.append("")
    collisions = registry.get("name_collisions") or []
    if collisions:
        for c in collisions:
            lines.append("- `%s` 出现在 %d 个路径：%s" % (c["name"], len(c["paths"]), "；".join(c["paths"])))
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("## 异常记录")
    lines.append("")
    errors = registry.get("errors") or []
    if errors:
        for e in errors:
            lines.append("- %s：%s" % (e.get("path"), e.get("error")))
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("## 未验证字段")
    lines.append("")
    lines.append("quality_score / security_level / update_status 当前均为 pending（阶段 4 起填充）。")
    lines.append("")
    lines.append("- source_type：builtin = 与内置技能源（%s）清单哈希一致；builtin-shadow = 与内置同名但内容不同（本地同名遮蔽或内置技能被本地修改，仅凭文件无法进一步判定）；local = 内置源中无同名技能。" % bundled_dir)
    lines.append("- 分类优先级：frontmatter category → tags 推断 → 路径第一段（与 Hermes loader `_get_category_from_path` 一致）→ 未分类。")
    lines.append("- 扫描跳过目录与 Hermes 加载器一致（.git/.github/.hub/.archive/.curator_backups/.locks/.venv/venv/node_modules/site-packages/__pycache__ 等）。")
    lines.append("")
    return "\n".join(lines)


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


def main(argv=None):
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    ap = argparse.ArgumentParser(
        description="Hermes Skill 只读盘点脚本（SKM）")
    ap.add_argument("--skills-dir", default=os.path.join(hermes_home, "skills"))
    ap.add_argument("--bundled-dir", default=os.path.join(hermes_home, "hermes-agent", "skills"))
    ap.add_argument("--config-path", default=os.path.join(hermes_home, "config.yaml"))
    ap.add_argument("--out-dir", default=os.path.join(hermes_home, "skill-management", "registry"))
    ap.add_argument("--scan-log", default=os.path.join(hermes_home, "skill-management", "logs", "scan-log.md"))
    ap.add_argument("--project-roots", nargs="*", default=[],
                    help="已知项目根目录列表；检查 <root>/.hermes/skills 与 <root>/.agents/skills")
    ap.add_argument("--extra-dirs", nargs="*", default=[],
                    help="额外探测路径列表；缺失路径会记录为异常")
    args = ap.parse_args(argv)

    errors = []
    meta = {
        "generator": "scan_skills.py (SKM)",
        "generator_version": SCRIPT_VERSION,
        "scanned_at": utcnow_iso(),
        "scanned_by": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "roots": {},
    }

    # 1) 默认技能目录（已确认加载路径）
    sd = {"path": args.skills_dir, "status": "missing", "skill_count": 0}
    skills = {}
    if os.path.isdir(args.skills_dir):
        sd["status"] = "present"
        skills = scan_dir(args.skills_dir, errors)
        sd["skill_count"] = len(skills)
    else:
        errors.append({"path": args.skills_dir, "error": "默认技能目录不存在或不可访问"})
    meta["roots"]["default_skills_dir"] = sd

    # 2) 配置中的 external_dirs / create_dir（只读解析）
    cfg = read_skills_config(args.config_path, errors)
    if cfg["external_dirs"] is not None:
        meta["roots"]["external_dirs"] = {
            "status": "configured", "paths": cfg["external_dirs"],
            "note": "本版脚本仅记录配置，不扫描外部目录（如已启用请联系用户确认）"}
    else:
        meta["roots"]["external_dirs"] = {"status": "not_configured"}
    if cfg["create_dir"] is not None:
        meta["roots"]["create_dir"] = {"status": "configured", "path": cfg["create_dir"]}
    else:
        meta["roots"]["create_dir"] = {
            "status": "not_configured",
            "note": "依据 config.yaml 注释：技能创建默认写入 ~/.hermes/skills/"}
    meta["roots"]["config_notes"] = cfg["notes"]

    # 3) 项目级技能目录
    proots = []
    for root in args.project_roots:
        entry = {"path": root, "status": "present", "checks": {}}
        if not os.path.isdir(root):
            entry["status"] = "missing"
            errors.append({"path": root, "error": "项目根目录不存在或不可访问"})
        for sub in (".hermes/skills", ".agents/skills"):
            p = os.path.join(root, sub)
            if os.path.isdir(p):
                n = 0
                for _dp, _dn, fns in os.walk(p, followlinks=False):
                    n += sum(1 for fn in fns if fn == "SKILL.md")
                entry["checks"][sub] = "present（%d 个 SKILL.md）" % n
            else:
                entry["checks"][sub] = "missing"
        entry["details"] = "；".join("%s: %s" % (k, v) for k, v in entry["checks"].items())
        proots.append(entry)
    meta["roots"]["project_roots"] = {
        "status": "checked" if proots else "none_given", "roots": proots}

    # 4) 内置技能源（候选/参考路径，不纳入已安装技能清单）
    bundled_by_name = {}
    bcand = {"path": args.bundled_dir, "status": "missing",
             "note": "内置技能源，仅用于 source_type 比对，不纳入已安装技能清单"}
    if os.path.isdir(args.bundled_dir):
        bundled = scan_dir(args.bundled_dir, errors)
        for _p, rec in bundled.items():
            bundled_by_name[rec["name"]] = rec["sha256"]
        bcand["status"] = "present"
        bcand["skill_count"] = len(bundled)
    else:
        errors.append({"path": args.bundled_dir, "error": "内置技能源目录不存在或不可访问"})
    meta["roots"]["candidate_paths"] = [bcand]

    # 5) Curator 归档区（.archive 被加载器排除，归档技能单独登记、不计入已安装清单）
    arch_dir = os.path.join(args.skills_dir, ".archive")
    arch_entry = {"path": arch_dir, "status": "absent",
                  "note": "Curator 归档区；加载器排除 .archive，归档技能不计入已安装清单"}
    if os.path.isdir(arch_dir):
        n_arch = 0
        for _dp, _dn, fns in os.walk(arch_dir, followlinks=False):
            n_arch += sum(1 for fn in fns if fn == "SKILL.md")
        arch_entry["status"] = "present"
        arch_entry["archived_skill_count"] = n_arch
    meta["roots"]["curator_archive"] = arch_entry

    # 6) 额外探测路径
    extras = []
    for p in args.extra_dirs:
        if os.path.isdir(p):
            extras.append({"path": p, "status": "present"})
        else:
            extras.append({"path": p, "status": "missing"})
            errors.append({"path": p, "error": "额外探测路径不存在或不可访问"})
    meta["roots"]["extra_dirs"] = extras

    # 7) source_type 判定
    for _p, rec in skills.items():
        if rec["name"] in bundled_by_name:
            rec["source_type"] = ("builtin" if bundled_by_name[rec["name"]] == rec["sha256"]
                                  else "builtin-shadow")
        else:
            rec["source_type"] = "local"

    # 8) 重名冲突（按绝对路径建索引，不会互相覆盖；此处仅登记重名情况）
    by_name = {}
    for _p, rec in skills.items():
        by_name.setdefault(rec["name"], []).append(rec["path"])
    collisions = [{"name": n, "paths": ps} for n, ps in by_name.items() if len(ps) > 1]

    registry = {
        "scan_meta": meta,
        "skills": skills,
        "errors": errors,
        "name_collisions": collisions,
    }

    # 9) 输出（原子写入）
    os.makedirs(args.out_dir, exist_ok=True)
    json_path = os.path.join(args.out_dir, "skills.json")
    md_path = os.path.join(args.out_dir, "inventory.md")
    # 若存在上一版 Registry，先轮转为 .prev，供 compare_inventory.py 对比
    prev_path = os.path.join(args.out_dir, "skills.json.prev")
    if os.path.exists(json_path):
        try:
            os.replace(json_path, prev_path)
        except OSError:
            pass
    write_atomic(json_path, json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
    write_atomic(md_path, render_inventory_md(registry, args.skills_dir, args.bundled_dir) + "\n")

    # 10) 扫描日志
    log_line = "- [scan] %s 已安装技能 %d 个（%s），异常 %d 条\n" % (
        meta["scanned_at"], len(skills), args.skills_dir, len(errors))
    try:
        os.makedirs(os.path.dirname(args.scan_log), exist_ok=True)
        with open(args.scan_log, "a", encoding="utf-8") as f:
            f.write(log_line)
    except OSError as e:
        errors.append({"path": args.scan_log, "error": "扫描日志写入失败: %s" % e})

    print("扫描完成：技能 %d 个，异常 %d 条" % (len(skills), len(errors)))
    print("Registry: %s" % json_path)
    print("清单: %s" % md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
