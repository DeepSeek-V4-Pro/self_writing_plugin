"""从 LLM 响应中解析文件内容。"""

import re
from typing import Optional


_CODE_BLOCK_RE = re.compile(
    r"```(?P<lang>[^\n:]*)(?::(?P<filename>[^\n]*))?\s*\n(?P<code>.*?)```",
    re.DOTALL,
)

_MAX_RESPONSE_SIZE = 200 * 1024

_EXPECTED_FILES = {"_manifest.json", "plugin.py", "config.toml"}
_FILENAME_ALIASES = {
    "manifest.json": "_manifest.json",
    "manifest": "_manifest.json",
    "main.py": "plugin.py",
    "plugin": "plugin.py",
    "config": "config.toml",
    "settings.toml": "config.toml",
}


def parse_llm_response(response: str) -> dict[str, str]:
    files: dict[str, str] = {}

    if len(response) > _MAX_RESPONSE_SIZE:
        response = response[:_MAX_RESPONSE_SIZE]

    for match in _CODE_BLOCK_RE.finditer(response):
        filename = (match.group("filename") or "").strip()
        code = match.group("code").strip()

        if not code:
            continue

        resolved = _resolve_filename(filename, code)
        if not resolved:
            continue

        if resolved not in files:
            files[resolved] = code

    return files


def _resolve_filename(filename: str, code: str) -> Optional[str]:
    if filename in _EXPECTED_FILES:
        return filename

    canonical = _FILENAME_ALIASES.get(filename.lower())
    if canonical:
        return canonical

    for target in _EXPECTED_FILES:
        if target.endswith(filename):
            return target

    if not filename:
        if '"manifest_version"' in code[:300] or '"id":' in code[:300]:
            return "_manifest.json"
        if "create_plugin(" in code or "MaiBotPlugin" in code:
            return "plugin.py"
        if "[plugin]" in code[:200] or "enabled =" in code[:200]:
            return "config.toml"

    return None
