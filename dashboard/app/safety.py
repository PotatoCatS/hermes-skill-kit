"""Path whitelist for dashboard reads.

Only the management subdirectories and the skills directory are readable here.
Writes to proposal files go through the SKM bridge and the existing pipeline;
this module still exposes no write helper on purpose.

The three roots can be relocated through SKD_* environment variables so the
whole approval flow can be drilled against a /tmp fixture without touching the
real skills tree. Defaults follow HERMES_HOME or ~/.hermes.
"""
import os
from pathlib import Path

HERMES_HOME = Path(
    os.environ.get("SKD_HERMES_HOME")
    or os.environ.get("HERMES_HOME")
    or (Path.home() / ".hermes")
)
BASE = Path(os.environ.get("SKD_BASE_DIR", str(HERMES_HOME / "skill-management")))
SKILLS_DIR = Path(os.environ.get("SKD_SKILLS_DIR", str(HERMES_HOME / "skills")))

ALLOWED_ROOTS = {
    "reports": BASE / "reports",
    "audits": BASE / "audits",
    "proposals": BASE / "proposals",
    "registry": BASE / "registry",
    "skills": SKILLS_DIR,
}

TEXT_SUFFIXES = {".md", ".json", ".yaml", ".yml", ".txt"}
MAX_TEXT_BYTES = 512 * 1024


class UnsafePath(ValueError):
    pass


def safe_file(root_key: str, relative: str) -> Path:
    """Resolve `relative` under an allowed root, refusing traversal."""
    if root_key not in ALLOWED_ROOTS:
        raise UnsafePath(f"unknown root: {root_key}")
    root = ALLOWED_ROOTS[root_key].resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise UnsafePath("path escapes allowed root")
    if candidate.suffix.lower() not in TEXT_SUFFIXES:
        raise UnsafePath("unsupported file type")
    if not candidate.is_file():
        raise FileNotFoundError(relative)
    return candidate


def read_text(root_key: str, relative: str) -> str:
    path = safe_file(root_key, relative)
    data = path.read_bytes()
    if len(data) > MAX_TEXT_BYTES:
        data = data[:MAX_TEXT_BYTES]
    return data.decode("utf-8", errors="replace")


def skill_file(skill_path: str, name: str = "SKILL.md") -> Path:
    """Read a file inside one skill directory, refusing symlink escapes."""
    root = SKILLS_DIR.resolve()
    base = Path(skill_path).resolve()
    if root not in base.parents:
        raise UnsafePath("skill path outside skills dir")
    candidate = (base / name).resolve()
    if base not in candidate.parents:
        raise UnsafePath("skill file escapes skill dir")
    if not candidate.is_file():
        raise FileNotFoundError(name)
    return candidate
