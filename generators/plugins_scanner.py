"""扫描已有插件目录，构建参考目录。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent


def _resolve_plugins_dir() -> Path:
    if PLUGINS_DIR.is_dir():
        return PLUGINS_DIR
    alt = Path.cwd() / "plugins"
    if alt.is_dir():
        return alt
    return PLUGINS_DIR

EXCLUDED_PLUGINS: frozenset[str] = frozenset({
    "deepseek-v4-pro.self-writing-plugin",
    "maibot-team.napcat-adapter",
    "maibot-team.snowluma-adapter",
})

DECORATOR_PATTERNS = (
    ("@Command", "命令"),
    ("@Tool", "工具"),
    ("@EventHandler", "事件监听"),
    ("@HookHandler", "钩子"),
    ("@Action", "动作(已弃用)"),
    ("@MessageGateway", "网关"),
)

MAX_PLUGIN_PY_BYTES = 200 * 1024


class PluginInfo:
    __slots__ = (
        "id", "name", "version", "description",
        "capabilities", "deps_pip", "deps_plugin",
        "plugin_py_lines", "components",
    )

    def __init__(
        self,
        plugin_id: str = "",
        name: str = "",
        version: str = "",
        description: str = "",
        capabilities: Optional[list[str]] = None,
        deps_pip: Optional[list[str]] = None,
        deps_plugin: Optional[list[str]] = None,
        plugin_py_lines: int = 0,
        components: Optional[list[str]] = None,
    ):
        self.id = plugin_id
        self.name = name
        self.version = version
        self.description = description
        self.capabilities = capabilities or []
        self.deps_pip = deps_pip or []
        self.deps_plugin = deps_plugin or []
        self.plugin_py_lines = plugin_py_lines
        self.components = components or []

    def summary_line(self) -> str:
        parts = [f"[{self.id}] {self.name}"]
        if self.components:
            parts.append(f"({', '.join(self.components[:4])})")
        if self.capabilities:
            parts.append(f"能力:{','.join(self.capabilities[:5])}")
        return " ".join(parts)


def scan_all_plugins() -> dict[str, PluginInfo]:
    catalog: dict[str, PluginInfo] = {}
    plugins_path = _resolve_plugins_dir()

    if not plugins_path.is_dir():
        return catalog

    for entry in sorted(plugins_path.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "_manifest.json"
        if not manifest_path.is_file():
            continue

        try:
            info = _read_plugin(entry, manifest_path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue

        if info is None or info.id in EXCLUDED_PLUGINS:
            continue

        catalog[info.id] = info

    return catalog


def _read_plugin(plugin_dir: Path, manifest_path: Path) -> Optional[PluginInfo]:
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    plugin_id = str(manifest_data.get("id", "")).strip()
    if not plugin_id or plugin_id in EXCLUDED_PLUGINS:
        return None

    deps_pip: list[str] = []
    deps_plugin: list[str] = []
    for dep in manifest_data.get("dependencies", []) or []:
        if not isinstance(dep, dict):
            continue
        t = str(dep.get("type", "")).lower()
        n = str(dep.get("name", ""))
        if t == "pip":
            deps_pip.append(n)
        elif t in ("plugin", "adapter"):
            deps_plugin.append(n)

    plugin_py_path = plugin_dir / "plugin.py"
    lines = 0
    components: list[str] = []
    if plugin_py_path.is_file():
        try:
            raw = plugin_py_path.read_text(encoding="utf-8")
            lines = raw.count("\n")
            for pattern, label in DECORATOR_PATTERNS:
                if pattern in raw:
                    components.append(label)
        except (OSError, UnicodeDecodeError):
            pass

    return PluginInfo(
        plugin_id=plugin_id,
        name=str(manifest_data.get("name", "")),
        version=str(manifest_data.get("version", "")),
        description=str(manifest_data.get("description", "")),
        capabilities=manifest_data.get("capabilities", []),
        deps_pip=deps_pip,
        deps_plugin=deps_plugin,
        plugin_py_lines=lines,
        components=components,
    )


def read_plugin_source(plugin_id: str) -> Optional[str]:
    plugins_path = _resolve_plugins_dir()
    for entry in plugins_path.iterdir():
        if not entry.is_dir():
            continue
        manifest_path = entry / "_manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if data.get("id") != plugin_id:
            continue

        plugin_py = entry / "plugin.py"
        if plugin_py.is_file():
            try:
                content = plugin_py.read_text(encoding="utf-8")
                if len(content.encode("utf-8")) <= MAX_PLUGIN_PY_BYTES:
                    return content
            except (OSError, UnicodeDecodeError):
                pass
        break

    return None


def build_catalog_text(catalog: dict[str, PluginInfo]) -> str:
    if not catalog:
        return ""

    lines = [f"## 已有插件目录 ({len(catalog)} 个可用)\n"]
    for info in catalog.values():
        desc = (info.description or "")[:80]
        caps = ", ".join(info.capabilities[:6]) if info.capabilities else "无"
        comps = ", ".join(info.components[:4]) if info.components else "未分析"
        lines.append(
            f"- {info.id} | {info.name} | {info.plugin_py_lines}行 | "
            f"组件:{comps} | 能力:{caps} | {desc}"
        )
    lines.append("")
    return "\n".join(lines)
