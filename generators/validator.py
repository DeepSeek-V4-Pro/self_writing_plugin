"""多层级安全验证器 — 5 层保险。

层级:
  1. AST 语法 + 危险导入 + 危险调用 + 危险属性检查
  2. Capability 白名单 —— 禁止生成插件声明高危能力
  3. 复杂度限制 —— 行数/函数数/导入数/AST 节点数
  4. 正则模式扫描 —— 动态执行、网络请求、文件写入等
  5. 依赖限制 —— 禁止外部 pip 包依赖
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Optional

try:
    import tomllib
except ImportError:
    import tomli as tomllib

# ── 第 1 层: AST 安全 ──────────────────────────────────────────

DANGEROUS_FUNCTIONS = frozenset({
    "eval", "exec", "compile", "__import__",
    "getattr", "setattr", "delattr",
    "globals", "locals", "vars",
    "breakpoint",
})

DANGEROUS_ATTRIBUTES = frozenset({
    "__subclasses__", "__bases__", "__mro__",
    "__builtins__", "__globals__", "__code__",
    "__closure__", "__func__", "__self__",
})

DANGEROUS_BUILTIN_CALLS = frozenset({
    "open", "input",
})

# ── 第 2 层: Capability 白名单 ─────────────────────────────────

SAFE_CAPABILITY_WHITELIST: frozenset[str] = frozenset({
    "send.text",
    "send.image",
    "send.forward",
    "send.hybrid",
    "emoji.get_random",
    "emoji.get_by_keyword",
    "config.get",
    "message.get_recent",
    "message.get_by_time",
    "message.get_by_time_in_chat",
    "chat.get_group_streams",
    "chat.get_stream_by_group_id",
    "chat.get_stream_by_user_id",
    "frequency.get",
    "frequency.check",
    "person.get",
    "render.render",
    "api.call",
})

FORBIDDEN_CAPABILITY_PATTERNS: tuple[str, ...] = (
    "component.",
    "gateway.",
    "tool.",
    "knowledge.",
    "llm.",
)

# ── 第 3 层: 复杂度限制 ────────────────────────────────────────

DEFAULT_MAX_PLUGIN_LINES = 1000
DEFAULT_MAX_FUNCTIONS = 30
DEFAULT_MAX_IMPORTS = 20
DEFAULT_MAX_AST_NODES = 5000

# ── 第 4 层: 正则模式扫描 ──────────────────────────────────────

DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"\b__import__\s*\(", "禁止调用 __import__()"),
    (r"\beval\s*\(", "禁止调用 eval()"),
    (r"\bexec\s*\(", "禁止调用 exec()"),
    (r"\bcompile\s*\(.*\)\s*\n", "禁止调用 compile()（可能用于动态代码执行）"),
    (r"\bgetattr\s*\([^)]*\)", "禁止使用 getattr() 动态属性访问"),
    (r"\bsetattr\s*\([^)]*\)", "禁止使用 setattr() 动态属性设置"),
    (r"\bimportlib\b", "禁止使用 importlib 动态导入"),
    (r"\b(?:urllib|urllib2|urllib3)\b", "禁止使用 urllib 发起网络请求"),
    (r"\b(?:requests|httpx|aiohttp)\b", "禁止使用 requests/httpx/aiohttp 发起网络请求"),
    (r"\b(?:socket|websocket|websockets)\b", "禁止使用 socket/websocket 建立连接"),
    (r"\b(?:ftplib|smtplib|imaplib|poplib)\b", "禁止使用网络协议库"),
    (r"\b(?:subprocess|os\.system|os\.popen|os\.spawn)\b", "禁止调用系统命令"),
    (r"\b(?:shutil\.(?:rmtree|copytree|move)|os\.(?:remove|rmdir|unlink|rename))\b", "禁止文件/目录删除移动操作"),
    (r"\bopen\s*\([^)]*['\"][wa]\+?['\"]", "禁止以写入/追加模式打开文件"),
    (r"\bopen\s*\([^)]*['\"]x['\"]", "禁止以创建模式打开文件"),
    (r"\b(?:pickle|shelve|marshal)\.(?:load|dump)", "禁止反序列化不可信数据"),
    (r"\b(?:base64|binascii|codecs)\.decode\b", "禁止使用编码库进行可能代码混淆"),
    (r"\b(?:lambda|exec|eval).*\bcompile\b", "禁止 lambda+compile 组合"),
    (r"\bglobals\s*\(\s*\)", "禁止访问 globals()"),
    (r"\blocals\s*\(\s*\)", "禁止访问 locals()"),
    (r"(?:__subclasses__|__bases__|__mro__)", "禁止内省类层级结构"),
    (r"\b__builtins__\b", "禁止访问 __builtins__"),
    (r"\bctypes\b", "禁止使用 ctypes"),
    (r"\bmultiprocessing\b", "禁止使用 multiprocessing"),
]

# ── 第 5 层: 依赖限制 ──────────────────────────────────────────

SAFE_DEPENDENCY_PREFIXES: tuple[str, ...] = ("maibot_sdk", "maibot-")

# ── Plugin ID 安全格式（防止路径穿越）─────────────────────────

PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def is_safe_plugin_id(plugin_id: str) -> bool:
    return bool(PLUGIN_ID_RE.match(plugin_id))


# ── Manifest 必填字段 ──────────────────────────────────────────

REQUIRED_MANIFEST_FIELDS = [
    "id", "version", "name", "description", "manifest_version",
    "capabilities", "author", "license", "urls",
    "host_application", "sdk", "dependencies", "i18n",
]


def validate_plugin_files(
    files: dict[str, str],
    forbidden_imports: list[str],
    max_file_size: int,
) -> list[str]:
    errors: list[str] = []

    manifest_content = files.get("_manifest.json")
    plugin_content = files.get("plugin.py")
    config_content = files.get("config.toml")

    # ── 文件存在性 ──
    if manifest_content is None:
        errors.append("[文件] 缺少 _manifest.json")
    if plugin_content is None:
        errors.append("[文件] 缺少 plugin.py")
    if config_content is None:
        errors.append("[文件] 缺少 config.toml")

    # ── 第 2 层: Capability 白名单 ──
    if manifest_content:
        errors.extend(_validate_manifest(manifest_content, max_file_size))
        errors.extend(_validate_capability_whitelist(manifest_content))

    # ── 第 5 层: 依赖限制 ──
    if manifest_content:
        errors.extend(_validate_dependencies(manifest_content))

    # ── 第 1 层: Python AST 安全 ──
    if plugin_content:
        errors.extend(_validate_python_ast(plugin_content, forbidden_imports, max_file_size))

    # ── 第 3 层: 复杂度限制 ──
    if plugin_content:
        errors.extend(_validate_complexity(plugin_content))

    # ── 第 4 层: 正则模式扫描 ──
    if plugin_content:
        errors.extend(_validate_dangerous_patterns(plugin_content))

    # ── TOML 语法 ──
    if config_content:
        errors.extend(_validate_toml(config_content, max_file_size))

    return errors


# ══════════════════════════════════════════════════════════════════
# 第 1 层: AST 安全检查
# ══════════════════════════════════════════════════════════════════

def _validate_python_ast(
    content: str,
    forbidden_imports: list[str],
    max_size: int,
) -> list[str]:
    errors: list[str] = []
    prefix = "[AST] "

    if len(content.encode("utf-8")) > max_size:
        errors.append(f"{prefix}plugin.py 超过大小限制 ({max_size} bytes)")
        return errors

    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        errors.append(f"{prefix}plugin.py 语法错误: {e}")
        return errors

    forbidden = {imp.lower() for imp in forbidden_imports}

    for node in ast.walk(tree):
        _check_node(node, prefix, forbidden, errors)

    has_create_plugin = any(
        isinstance(node, ast.FunctionDef) and node.name == "create_plugin"
        for node in ast.walk(tree)
    )
    if not has_create_plugin:
        errors.append(f"{prefix}plugin.py 缺少 create_plugin() 函数")

    return errors


def _check_node(
    node: ast.AST,
    prefix: str,
    forbidden: set[str],
    errors: list[str],
) -> None:
    # 1a. 禁止导入检查
    if isinstance(node, ast.Import):
        for alias in node.names:
            name = alias.name.split(".")[0].lower()
            if name in forbidden:
                errors.append(f"{prefix}禁止导入: {alias.name}")

    elif isinstance(node, ast.ImportFrom):
        if node.module:
            name = node.module.split(".")[0].lower()
            if name in forbidden:
                errors.append(f"{prefix}禁止导入: {node.module}")

    # 1b. 危险函数调用检查
    elif isinstance(node, ast.Call):
        func_name = _get_call_name(node)
        if func_name in DANGEROUS_FUNCTIONS:
            errors.append(f"{prefix}禁止调用: {func_name}()")

        if func_name in DANGEROUS_BUILTIN_CALLS:
            if func_name == "open":
                args = node.args
                kw_mode = None
                mode_is_variable = False
                for kw in node.keywords:
                    if kw.arg == "mode":
                        if isinstance(kw.value, ast.Constant):
                            kw_mode = str(kw.value.value)
                        else:
                            mode_is_variable = True
                if not kw_mode and len(args) >= 2:
                    if isinstance(args[1], ast.Constant):
                        kw_mode = str(args[1].value)
                    else:
                        mode_is_variable = True
                if kw_mode and any(c in kw_mode for c in "wax+"):
                    errors.append(f"{prefix}禁止以写入模式调用 open(): mode={kw_mode}")
                elif mode_is_variable:
                    errors.append(f"{prefix}禁止 open() 使用变量 mode 参数（安全风险）")
                elif not kw_mode and len(args) == 1:
                    pass

    # 1c. 危险属性访问检查
    elif isinstance(node, ast.Attribute):
        if node.attr in DANGEROUS_ATTRIBUTES:
            errors.append(f"{prefix}禁止访问危险属性: .{node.attr}")


def _get_call_name(node: ast.Call) -> Optional[str]:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


# ══════════════════════════════════════════════════════════════════
# 第 2 层: Capability 白名单
# ══════════════════════════════════════════════════════════════════

def _validate_capability_whitelist(manifest_content: str) -> list[str]:
    errors: list[str] = []
    prefix = "[权限] "

    try:
        data = json.loads(manifest_content)
    except json.JSONDecodeError:
        return errors

    capabilities = data.get("capabilities", [])
    if not isinstance(capabilities, list):
        return errors

    for cap in capabilities:
        cap_str = str(cap)
        if cap_str in SAFE_CAPABILITY_WHITELIST:
            continue

        for pattern in FORBIDDEN_CAPABILITY_PATTERNS:
            if cap_str.startswith(pattern):
                errors.append(f"{prefix}禁止声明高危能力: {cap_str}")
                break
        else:
            errors.append(f"{prefix}未知能力不在白名单中: {cap_str}")

    return errors


# ══════════════════════════════════════════════════════════════════
# 第 3 层: 复杂度限制
# ══════════════════════════════════════════════════════════════════

def _validate_complexity(content: str) -> list[str]:
    errors: list[str] = []
    prefix = "[复杂度] "

    lines = content.split("\n")
    line_count = len(lines)
    if line_count > DEFAULT_MAX_PLUGIN_LINES:
        errors.append(f"{prefix}行数 {line_count} 超过上限 {DEFAULT_MAX_PLUGIN_LINES}")

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return errors

    func_count = sum(1 for node in ast.walk(tree) if isinstance(node, ast.FunctionDef))
    if func_count > DEFAULT_MAX_FUNCTIONS:
        errors.append(f"{prefix}函数数 {func_count} 超过上限 {DEFAULT_MAX_FUNCTIONS}")

    import_count = sum(
        1 for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    )
    if import_count > DEFAULT_MAX_IMPORTS:
        errors.append(f"{prefix}导入数 {import_count} 超过上限 {DEFAULT_MAX_IMPORTS}")

    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > DEFAULT_MAX_AST_NODES:
        errors.append(f"{prefix}AST 节点数 {node_count} 超过上限 {DEFAULT_MAX_AST_NODES}")

    return errors


# ══════════════════════════════════════════════════════════════════
# 第 4 层: 正则模式扫描
# ══════════════════════════════════════════════════════════════════

def _validate_dangerous_patterns(content: str) -> list[str]:
    errors: list[str] = []
    prefix = "[模式] "

    for pattern, message in DANGEROUS_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE | re.MULTILINE):
            errors.append(f"{prefix}{message}")

    return errors


# ══════════════════════════════════════════════════════════════════
# 第 5 层: 依赖限制
# ══════════════════════════════════════════════════════════════════

def _validate_dependencies(manifest_content: str) -> list[str]:
    errors: list[str] = []
    prefix = "[依赖] "

    try:
        data = json.loads(manifest_content)
    except json.JSONDecodeError:
        return errors

    dependencies = data.get("dependencies", [])
    if not isinstance(dependencies, list):
        return errors

    for dep in dependencies:
        if not isinstance(dep, dict):
            continue
        dep_type = str(dep.get("type", "")).lower()
        dep_name = str(dep.get("name", ""))

        if dep_type == "pip":
            if not dep_name.startswith(SAFE_DEPENDENCY_PREFIXES):
                errors.append(f"{prefix}禁止外部 pip 依赖: {dep_name}")
        elif dep_type in ("plugin", "adapter"):
            errors.append(f"{prefix}生成插件禁止依赖其他插件/适配器: {dep_name}")

    return errors


# ══════════════════════════════════════════════════════════════════
# Manifest 基础校验
# ══════════════════════════════════════════════════════════════════

def _validate_manifest(content: str, max_size: int) -> list[str]:
    errors: list[str] = []
    prefix = "[Manifest] "

    if len(content.encode("utf-8")) > max_size:
        errors.append(f"{prefix}超过大小限制 ({max_size} bytes)")
        return errors

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        errors.append(f"{prefix}JSON 解析失败: {e}")
        return errors

    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in data:
            errors.append(f"{prefix}缺少必填字段: {field}")

    if "id" in data:
        plugin_id = data["id"]
        if not isinstance(plugin_id, str) or "." not in plugin_id:
            errors.append(f"{prefix}id 格式不正确，应为 author.plugin-name")
        elif " " in plugin_id:
            errors.append(f"{prefix}id 不能包含空格")

    if "capabilities" in data and not isinstance(data.get("capabilities"), list):
        errors.append(f"{prefix}capabilities 必须是数组")

    if "author" in data and not isinstance(data.get("author"), dict):
        errors.append(f'{prefix}author 必须是对象: {{"name": "...", "url": "..."}}')

    if "urls" in data and not isinstance(data.get("urls"), dict):
        errors.append(f"{prefix}urls 必须是对象，包含 repository/homepage/documentation/issues")

    if "license" in data and not isinstance(data.get("license"), str):
        errors.append(f"{prefix}license 必须是字符串")

    return errors


# ══════════════════════════════════════════════════════════════════
# TOML 校验
# ══════════════════════════════════════════════════════════════════

def _validate_toml(content: str, max_size: int) -> list[str]:
    errors: list[str] = []
    prefix = "[TOML] "

    if len(content.encode("utf-8")) > max_size:
        errors.append(f"{prefix}超过大小限制 ({max_size} bytes)")
        return errors

    try:
        tomllib.loads(content)
    except (ValueError, TypeError, LookupError) as e:
        errors.append(f"{prefix}TOML 解析失败: {e}")

    return errors


# ── 辅助 ────────────────────────────────────────────────────────

def extract_plugin_id(files: dict[str, str]) -> Optional[str]:
    manifest = files.get("_manifest.json")
    if not manifest:
        return None
    try:
        data = json.loads(manifest)
        return str(data.get("id", "")).strip() or None
    except json.JSONDecodeError:
        return None


def compute_files_hash(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(files.keys()):
        h.update(name.encode())
        h.update(files[name].encode())
    return h.hexdigest()[:16]
