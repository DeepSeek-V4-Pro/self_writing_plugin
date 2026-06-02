"""插件自生成器 — 让麦麦根据自然语言描述自动生成插件代码。

安全设计 (6 层保险):
  1. AST 语法 + 危险导入/函数/属性检测
  2. Capability 白名单 —— 禁止生成插件声明高危能力
  3. 代码复杂度限制 —— 行数/函数数/导入数/节点数
  4. 正则模式扫描 —— 动态执行、网络请求、文件操作
  5. 依赖限制 —— 禁止外部 pip 包 + 禁止依赖其他插件
  6. 审计水印 —— 注入生成来源标记 + SHA256 哈希
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from maibot_sdk import Command, EventHandler, Field, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import EventType

from .generators import (
    PluginInfo,
    build_analyze_prompt,
    build_catalog_text,
    build_fix_error_prompt,
    build_fix_prompt,
    build_generation_prompt,
    build_modify_prompt,
    parse_llm_response,
    read_plugin_source,
    scan_all_plugins,
    validate_plugin_files,
)
from .generators.llm_client import create_llm_client
from .generators.validator import compute_files_hash, extract_plugin_id, is_safe_plugin_id

PLUGIN_BASE_DIR = Path(__file__).resolve().parent
STAGING_DIR = PLUGIN_BASE_DIR / "staging"
STAGING_MAX_AGE_SECONDS = 3600
PREVIEW_MAX_LINES = 20
WATERMARK_VERSION = "0.2.1"
PLUGINS_ROOT = PLUGIN_BASE_DIR.parent.resolve()


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用插件")
    config_version: str = Field(default="0.2.1", description="配置版本")
    access_token: str = Field(
        default="",
        description="访问令牌。填入任意非空值即视为已阅读并同意 README.md 中全部安全警告与免责声明。留空则拒绝所有操作。",
    )


class GenerationConfig(PluginConfigBase):
    __ui_label__ = "生成"
    __ui_icon__ = "cpu"
    __ui_order__ = 1

    model: str = Field(default="replyer", description="LLM 模型任务名（如 replyer/utils/planner），对应 model_task_config 中的键")
    model_temperature: float = Field(default=0.3, description="LLM 温度参数（当前版本由 prompt 控制）")
    max_retries: int = Field(default=2, description="验证失败后最大重试次数", ge=0, le=5)
    output_base_dir: str = Field(default="plugins/generated", description="生成插件的输出目录")
    llm_timeout_seconds: int = Field(default=25, description="LLM 调用超时时间(秒)，需小于平台 cap.call 30s 限制", ge=10, le=30)


class SafetyConfig(PluginConfigBase):
    __ui_label__ = "安全"
    __ui_icon__ = "shield"
    __ui_order__ = 2

    require_confirmation: bool = Field(default=True, description="必须预览确认后才能安装")
    forbidden_imports: str = Field(
        default="os,subprocess,socket,sys,ctypes,multiprocessing,pickle,marshal,importlib",
        description="禁止导入的模块列表，逗号分隔",
    )
    max_file_size_bytes: int = Field(default=204800, description="单个文件最大大小(字节)", ge=1024)


class CustomApiConfig(PluginConfigBase):
    __ui_label__ = "自定义 API"
    __ui_icon__ = "globe"
    __ui_order__ = 3

    enabled: bool = Field(default=False, description="启用自定义 API（替代内置 LLM）")
    api_url: str = Field(default="", description="API 端点 (OpenAI 兼容格式)，如 https://api.openai.com/v1")
    api_key: str = Field(default="", description="API 密钥 (以 sk- 或 Bearer 开头)")
    model: str = Field(default="", description="自定义 API 使用的模型名，如 gpt-4o / claude-3-5-sonnet")


class OperatorWhitelistConfig(PluginConfigBase):
    __ui_label__ = "操作员白名单"
    __ui_icon__ = "users"
    __ui_order__ = 4

    enabled: bool = Field(default=False, description="启用操作员白名单（仅名单内用户可用命令）")
    allowed_users: str = Field(
        default="",
        description="允许使用插件的用户 ID，逗号分隔。留空且 enabled=true 则拒绝所有用户",
    )
    allowed_streams: str = Field(
        default="",
        description="允许使用插件的聊天流 ID，逗号分隔。留空则不限制聊天流",
    )
    reject_message: str = Field(
        default="你没有权限使用此插件，请联系管理员将你的 ID 加入白名单",
        description="非白名单用户被拒绝时的提示消息",
    )


class SelfWritingPluginConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    custom_api: CustomApiConfig = Field(default_factory=CustomApiConfig)
    operator_whitelist: OperatorWhitelistConfig = Field(default_factory=OperatorWhitelistConfig)


class SelfWritingPlugin(MaiBotPlugin):
    config_model = SelfWritingPluginConfig

    async def on_load(self) -> None:
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        self._staged: dict[str, dict[str, Any]] = {}
        self._catalog: dict[str, PluginInfo] = {}
        self._catalog_text: str = ""
        self._last_generation_time: float = 0
        self._generation_cooldown: int = 10
        self._busy: bool = False
        self._generation_cancelled: bool = False
        self._generation_task: Optional[asyncio.Task[Any]] = None
        self._cleanup_stale_staging()
        self._restore_staging_from_disk()
        self._refresh_catalog()

    async def on_unload(self) -> None:
        self._staged.clear()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data, version

    # ── 忙碌拦截 ──────────────────────────────────────────────

    _INTERRUPT_COMMANDS = frozenset({"/abort", "/reject_plugin", "/list_staged"})

    @EventHandler(
        "busy_intercept",
        description="忙碌状态下拦截所有非操作员消息及非中断指令",
        event_type=EventType.ON_MESSAGE,
    )
    async def handle_busy_intercept(self, message: Any = None, stream_id: str = "", **kwargs: Any):
        if not self._busy:
            return True, True, None, None, None

        raw_msg = ""
        if isinstance(message, dict):
            raw_msg = str(message.get("plain_text") or message.get("raw_message") or "")
        elif isinstance(message, str):
            raw_msg = message
        raw_msg = raw_msg.strip()

        is_interrupt = any(raw_msg.startswith(cmd) for cmd in self._INTERRUPT_COMMANDS)

        if not is_interrupt:
            if not self._is_operator(stream_id, kwargs):
                await self.ctx.send.text(
                    "插件正在生成中，请稍候...（仅操作员可操作）",
                    stream_id,
                )
                return {"blocked": True}

            await self.ctx.send.text(
                f"插件正在生成中，仅允许中断指令: {', '.join(sorted(self._INTERRUPT_COMMANDS))}",
                stream_id,
            )
            return {"blocked": True}

        return True, True, None, None, None

    # ── 内部辅助 ──────────────────────────────────────────────

    def _check_whitelist(self, stream_id: str, kwargs: dict[str, Any]) -> Optional[tuple[bool, str, bool]]:
        wl = self.config.operator_whitelist
        if not wl.enabled:
            return None

        user_id = self._extract_user_id(kwargs)

        allowed_users = {u.strip() for u in wl.allowed_users.split(",") if u.strip()}
        allowed_streams = {s.strip() for s in wl.allowed_streams.split(",") if s.strip()}

        if allowed_streams and stream_id in allowed_streams:
            return None

        if allowed_users and user_id and user_id in allowed_users:
            return None

        user_desc = f"用户 {user_id}" if user_id else f"聊天流 {stream_id}"
        return False, f"{user_desc} {wl.reject_message}", True

    async def _check_access(self, stream_id: str) -> Optional[tuple[bool, str, bool]]:
        token = (self.config.plugin.access_token or "").strip()
        if not token:
            await self.ctx.send.text(
                "⛔ 访问令牌未设置，插件拒绝操作。\n\n"
                "请先阅读 plugins/self_writing_plugin/README.md 中全部安全警告与免责声明，"
                "然后在 config.toml 的 [plugin] 节中设置 access_token。",
                stream_id,
            )
            return False, "访问令牌未设置", True
        return None

    def _is_operator(self, stream_id: str, kwargs: dict[str, Any]) -> bool:
        wl = self.config.operator_whitelist
        if not wl.enabled:
            return True

        user_id = self._extract_user_id(kwargs)

        allowed_streams = {s.strip() for s in wl.allowed_streams.split(",") if s.strip()}
        if allowed_streams and stream_id in allowed_streams:
            return True

        allowed_users = {u.strip() for u in wl.allowed_users.split(",") if u.strip()}
        if allowed_users and user_id and user_id in allowed_users:
            return True

        return False

    @staticmethod
    def _extract_user_id(kwargs: dict[str, Any]) -> str:
        user_id = (
            kwargs.get("sender_id")
            or kwargs.get("user_id")
            or kwargs.get("operator")
            or ""
        )
        if isinstance(user_id, dict):
            user_id = str(user_id.get("user_id") or user_id.get("id") or "")
        return str(user_id).strip()

    def _abort_generation(self) -> None:
        self._generation_cancelled = True
        task = self._generation_task
        if task and not task.done():
            task.cancel()

    def _enter_busy(self) -> None:
        self._busy = True
        self._generation_cancelled = False

    def _leave_busy(self) -> None:
        self._busy = False
        self._generation_cancelled = False

    def _refresh_catalog(self) -> None:
        self._catalog = scan_all_plugins()
        self._catalog_text = build_catalog_text(self._catalog)

    def _restore_staging_from_disk(self) -> None:
        if not STAGING_DIR.exists():
            return
        for item in STAGING_DIR.iterdir():
            if not item.is_dir():
                continue
            meta_path = item / "_meta.json"
            if not meta_path.is_file():
                continue
            try:
                import json
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            pid = str(meta.get("id", "")).strip()
            if not pid:
                continue
            files: dict[str, str] = {}
            for fname in meta.get("files", []):
                fp = item / fname
                if fp.is_file():
                    try:
                        files[fname] = fp.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
            if not files:
                continue
            self._staged[pid] = {
                "files": files,
                "created_at": float(meta.get("created_at", time.time())),
                "description": str(meta.get("description", "")),
                "hash": str(meta.get("hash", "")),
            }

    def _cleanup_stale_staging(self) -> None:
        now = time.time()
        stale_ids: set[str] = set()
        if STAGING_DIR.exists():
            for item in STAGING_DIR.iterdir():
                if item.is_dir():
                    try:
                        if now - item.stat().st_mtime > STAGING_MAX_AGE_SECONDS:
                            shutil.rmtree(item, ignore_errors=True)
                            stale_ids.add(item.name)
                    except OSError:
                        pass
        for pid in stale_ids:
            self._staged.pop(pid, None)

    @staticmethod
    def _extract_arg(kwargs: dict[str, Any], pattern: str) -> Optional[str]:
        matched = kwargs.get("matched_groups")
        if isinstance(matched, dict):
            for key in ("text", "plugin_id", 0, "0"):
                val = matched.get(key)
                if val:
                    return str(val).strip()

        raw_text = str(kwargs.get("text", ""))
        m = re.match(pattern, raw_text, re.DOTALL)
        if m:
            return m.group(1).strip()
        return None

    @staticmethod
    def _rmtree(path: Path) -> None:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    # ── 生成核心 ──────────────────────────────────────────────

    def _read_plugin_from_anywhere(self, plugin_id: str) -> Optional[dict[str, str]]:
        staged = self._staged.get(plugin_id)
        if staged:
            return dict(staged.get("files", {}))

        output_base = Path(self.config.generation.output_base_dir)
        if not output_base.is_absolute():
            output_base = PLUGINS_ROOT / output_base
        install_dir = (output_base / plugin_id).resolve()
        try:
            install_dir.relative_to(PLUGINS_ROOT)
        except ValueError:
            return None

        if not install_dir.is_dir():
            return None

        files: dict[str, str] = {}
        for fname in ("_manifest.json", "plugin.py", "config.toml"):
            fp = install_dir / fname
            if fp.is_file():
                    try:
                        files[fname] = fp.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
        return files if len(files) >= 2 else None

    async def _attempt_generation(
        self,
        description: str,
        stream_id: str,
        gen_cfg: GenerationConfig,
        forbidden: list[str],
        max_file_size: int,
        reference_plugin_id: str = "",
        previous_errors: Optional[list[str]] = None,
    ) -> list[str]:
        ref_code = ""
        ref_name = ""
        if reference_plugin_id and reference_plugin_id != "none":
            ref_code = read_plugin_source(reference_plugin_id) or ""
            if ref_code:
                ref_name = reference_plugin_id
            else:
                return [f"参考插件 {reference_plugin_id} 不存在或无法读取"]

        if previous_errors:
            prompt = build_fix_prompt(
                description,
                previous_errors,
                catalog_text=self._catalog_text,
                reference_code=ref_code,
                reference_name=ref_name,
            )
        else:
            prompt = build_generation_prompt(
                description,
                catalog_text=self._catalog_text,
                reference_code=ref_code,
                reference_name=ref_name,
            )

        if self._generation_cancelled:
            return ["生成已被取消"]

        api_cfg = self.config.custom_api
        client = create_llm_client(
            self.ctx,
            custom_api_enabled=api_cfg.enabled,
            custom_api_url=api_cfg.api_url,
            custom_api_key=api_cfg.api_key,
            custom_api_model=api_cfg.model,
        )

        model_name = api_cfg.model if api_cfg.enabled else gen_cfg.model
        result = await client.generate(
            prompt=prompt,
            timeout=gen_cfg.llm_timeout_seconds,
            model=model_name,
        )

        if not result.success:
            return [result.error or "LLM 调用失败"]

        response = result.response
        if not response.strip():
            return ["LLM 返回空响应"]

        files = parse_llm_response(response)
        if not files:
            return ["无法从 LLM 响应中解析出插件文件，请确认模型是否正确输出代码块"]

        plugin_id = extract_plugin_id(files)
        if not plugin_id:
            return ["生成的 manifest 缺少有效的插件 ID"]
        if not is_safe_plugin_id(plugin_id):
            return [f"插件 ID 不安全: {plugin_id}（只能使用小写字母、数字、点号、下划线、连字符）"]

        errors = validate_plugin_files(files, forbidden, max_file_size)
        if errors:
            return errors

        files = self._inject_watermark(files, plugin_id, description)
        file_hash = compute_files_hash(files)
        staged_at = time.time()

        self._staged[plugin_id] = {
            "files": files,
            "created_at": staged_at,
            "description": description,
            "hash": file_hash,
        }

        self._save_staging_files(plugin_id, files, staged_at, description, file_hash)
        await self._send_preview(plugin_id, files, file_hash, stream_id)
        return []

    def _inject_watermark(
        self, files: dict[str, str], plugin_id: str, description: str
    ) -> dict[str, str]:
        gen_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        desc_short = description[:100]
        watermark = (
            "# ═══════════════════════════════════════════════════════\n"
            f"# Generated by MaiBot Self-Writing Plugin v{WATERMARK_VERSION}\n"
            f"# Plugin ID : {plugin_id}\n"
            f"# Generated : {gen_time}\n"
            f"# Description: {desc_short}\n"
            "# ═══════════════════════════════════════════════════════\n"
            "# 注意：此插件由 AI 自动生成，使用前请仔细审查代码安全性\n"
            f"# 安装位置: plugins/generated/{plugin_id}/\n"
            "# 默认处于禁用状态，需手动设置 enabled=true 启用\n"
            "# ═══════════════════════════════════════════════════════\n"
        )

        new_files = dict(files)
        plugin_py = new_files.get("plugin.py", "")

        if plugin_py.lstrip().startswith('"""'):
            lines = plugin_py.split("\n")
            doc_end = 0
            quote_seen = 0
            for i, line in enumerate(lines):
                count = line.count('"""')
                quote_seen += count
                if count > 0 and quote_seen >= 2:
                    doc_end = i
                    break
                if count > 0 and quote_seen == 1 and i > 0:
                    doc_end = i
                    break
            body = "\n".join(lines[doc_end + 1 :]) if doc_end < len(lines) else ""
            new_files["plugin.py"] = f"{watermark}\n{body}"
        else:
            new_files["plugin.py"] = f"{watermark}\n{plugin_py}"

        return new_files

    def _save_staging_files(
        self, plugin_id: str, files: dict[str, str],
        staged_at: float, description: str, file_hash: str,
    ) -> None:
        plugin_staging = STAGING_DIR / plugin_id
        plugin_staging.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            (plugin_staging / filename).write_text(content, encoding="utf-8")

        import json
        meta = {
            "id": plugin_id,
            "created_at": staged_at,
            "description": description,
            "hash": file_hash,
            "files": list(files.keys()),
        }
        (plugin_staging / "_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def _send_preview(
        self, plugin_id: str, files: dict[str, str], file_hash: str, stream_id: str
    ) -> None:
        file_sizes: dict[str, int] = {}
        for name in sorted(files.keys()):
            file_sizes[name] = len(files[name].encode("utf-8"))

        size_summary = "\n".join(f"  {name} ({s}B)" for name, s in file_sizes.items())

        await self.ctx.send.text(
            f"✅ 插件已生成，等待确认安装\n\n"
            f"插件 ID: {plugin_id}\n"
            f"SHA256  : {file_hash}\n"
            f"有效时间: 1 小时（超时自动清理）\n"
            f"安全验证: 已通过 6 层安全检查\n"
            f"文件列表:\n{size_summary}\n\n"
            f"📌 /confirm_plugin {plugin_id}  确认安装\n"
            f"🗑 /reject_plugin {plugin_id}  放弃\n"
            f"👁 /view_plugin {plugin_id}  查看完整代码\n"
            f"📋 /list_staged  查看全部暂存",
            stream_id,
        )

        forward_nodes: list[dict[str, Any]] = []
        for name in sorted(files.keys()):
            content = files[name]
            lines = content.split("\n")
            total_lines = len(lines)
            preview = "\n".join(lines[:PREVIEW_MAX_LINES])
            if total_lines > PREVIEW_MAX_LINES:
                preview += f"\n... (共 {total_lines} 行，仅展示前 {PREVIEW_MAX_LINES} 行)"

            forward_nodes.append({
                "user_id": "0",
                "nickname": f"{name} ({total_lines}行, {file_sizes[name]}B)",
                "segments": [{"type": "text", "content": preview}],
            })

        await self.ctx.send.forward(forward_nodes, stream_id)

    # ── Command: /write_plugin ─────────────────────────────────

    @Command(
        "write_plugin",
        description="根据自然语言描述生成 MaiBot 插件，支持 --from <插件ID> 指定参考",
        pattern=r"^/write_plugin\s+(?P<text>.+)$",
    )
    async def handle_write_plugin(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件自生成器未启用，请在配置中启用", True
        if denied := await self._check_access(stream_id):
            return denied
        if denied := self._check_whitelist(stream_id, kwargs):
            return denied

        if self._busy:
            await self.ctx.send.text(
                "插件正在生成中，请使用 /abort 打断后再开始新生成",
                stream_id,
            )
            return False, "忙碌中", True

        raw = self._extract_arg(kwargs, r"^/write_plugin\s+(.+)$") or ""
        description = raw
        reference_plugin_id = ""

        from_match = re.match(r"^(.+?)\s+--from\s+([^\s]+)\s*$", raw, re.DOTALL)
        if not from_match:
            from_match = re.match(r"^(.+?)\s+参考[:：]\s*([^\s]+)\s*$", raw, re.DOTALL)
        if from_match:
            description = from_match.group(1).strip()
            reference_plugin_id = from_match.group(2).strip()

        if not description or len(description) < 3:
            await self.ctx.send.text(
                "用法: /write_plugin <插件功能描述>\n"
                "      /write_plugin <描述> --from <已有插件ID>\n"
                "      /write_plugin <描述> 参考:<已有插件ID>\n\n"
                "例如: /write_plugin 帮我写一个每天定时发早安消息的插件\n"
                "      /write_plugin 类似头像插件但支持多图 --from deepseek-v4-pro.avatar-fetcher-plugin\n"
                "      查看已有插件: /list_plugins",
                stream_id,
            )
            return False, "描述太短", True

        hint = ""
        if reference_plugin_id:
            if reference_plugin_id not in self._catalog:
                await self.ctx.send.text(
                    f"参考插件 {reference_plugin_id} 不在已知目录中\n使用 /list_plugins 查看可用插件",
                    stream_id,
                )
                return False, "参考插件不存在", True
            info = self._catalog[reference_plugin_id]
            hint = f" (参考: {info.name})"
            await self.ctx.send.text(f"📖 参考插件: {info.name} ({reference_plugin_id})", stream_id)

        await self.ctx.send.text(f"🔧 收到需求{hint}，正在调用 LLM 生成插件代码...", stream_id)

        now_ts = time.time()
        if now_ts - self._last_generation_time < self._generation_cooldown:
            remaining = int(self._generation_cooldown - (now_ts - self._last_generation_time))
            await self.ctx.send.text(
                f"请稍等 {remaining} 秒后再生成（冷却中，防止滥用）",
                stream_id,
            )
            return False, f"冷却中，剩余 {remaining} 秒", True
        self._last_generation_time = now_ts

        gen_cfg = self.config.generation
        safety_cfg = self.config.safety
        forbidden = [s.strip().lower() for s in safety_cfg.forbidden_imports.split(",") if s.strip()]

        self._enter_busy()
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._run_generation_background(
                description, stream_id, reference_plugin_id,
                gen_cfg, forbidden, safety_cfg.max_file_size_bytes,
            )
        )
        self._generation_task = task
        return True, "生成任务已启动，完成后会通知你", True

    async def _run_generation_background(
        self,
        description: str,
        stream_id: str,
        reference_plugin_id: str,
        gen_cfg: GenerationConfig,
        forbidden: list[str],
        max_file_size: int,
    ) -> None:
        try:
            last_errors: list[str] = []
            for attempt in range(gen_cfg.max_retries + 1):
                if self._generation_cancelled:
                    await self.ctx.send.text("生成已被取消", stream_id)
                    return

                last_errors = await self._attempt_generation(
                    description, stream_id, gen_cfg, forbidden,
                    max_file_size, reference_plugin_id,
                    previous_errors=last_errors if attempt > 0 else None,
                )
                if not last_errors:
                    return

                if self._generation_cancelled:
                    await self.ctx.send.text("生成已被取消", stream_id)
                    return

                if attempt < gen_cfg.max_retries:
                    await self.ctx.send.text(
                        f"⚠ 第 {attempt + 1} 次生成有误，正在重试...\n错误: {last_errors[0]}",
                        stream_id,
                    )

            error_text = "\n".join(f"  - {e}" for e in last_errors[-5:])
            await self.ctx.send.text(
                f"❌ 插件生成失败，经过 {gen_cfg.max_retries + 1} 次重试仍有以下错误:\n{error_text}\n\n请简化需求后重试",
                stream_id,
            )
        except asyncio.CancelledError:
            await self.ctx.send.text("生成已被取消", stream_id)
        except Exception as e:
            await self.ctx.send.text(f"❌ 生成异常: {e}", stream_id)
        finally:
            self._leave_busy()
            self._generation_task = None

    # ── Command: /list_plugins ─────────────────────────────────

    @Command(
        "list_plugins",
        description="列出所有可用作参考的已有插件",
        pattern=r"^/list_plugins$",
    )
    async def handle_list_plugins(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件自生成器未启用", True
        if denied := await self._check_access(stream_id):
            return denied
        if denied := self._check_whitelist(stream_id, kwargs):
            return denied

        del kwargs

        self._refresh_catalog()

        if not self._catalog:
            await self.ctx.send.text("未发现可参考的已有插件", stream_id)
            return True, "目录为空", True

        lines: list[str] = [f"📚 可用作参考的插件 ({len(self._catalog)} 个):\n"]
        for info in self._catalog.values():
            desc = (info.description or "")[:60]
            comps = "/".join(info.components[:4]) if info.components else "基础"
            caps = ", ".join(info.capabilities[:4]) if info.capabilities else "无"
            lines.append(f"  [{info.id}]")
            lines.append(f"    名称: {info.name}  v{info.version}")
            lines.append(f"    组件: {comps}  |  行数: {info.plugin_py_lines}")
            if caps != "无":
                lines.append(f"    能力: {caps}")
            if desc:
                lines.append(f"    描述: {desc}")
            lines.append("")

        joined = "\n".join(lines)
        CHUNK = 2000
        node_parts: list[str] = []
        for i in range(0, len(joined), CHUNK):
            node_parts.append(joined[i : i + CHUNK])

        forward_nodes: list[dict[str, Any]] = []
        for idx, part in enumerate(node_parts):
            forward_nodes.append({
                "user_id": "0",
                "nickname": f"插件目录 ({idx + 1}/{len(node_parts)})" if len(node_parts) > 1 else "插件目录",
                "segments": [{"type": "text", "content": part}],
            })

        await self.ctx.send.forward(forward_nodes, stream_id)

        await self.ctx.send.text(
            f"用法: /write_plugin <描述> --from <插件ID>\n"
            f"例如: /write_plugin 类似当前插件 --from deepseek-v4-pro.self-writing-plugin",
            stream_id,
        )
        return True, f"共 {len(self._catalog)} 个参考插件", True

    # ── Command: /confirm_plugin ───────────────────────────────

    @Command(
        "confirm_plugin",
        description="确认安装暂存中的插件到目标目录",
        pattern=r"^/confirm_plugin\s+(?P<plugin_id>[^\s]+)$",
    )
    async def handle_confirm_plugin(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件自生成器未启用", True
        if denied := await self._check_access(stream_id):
            return denied
        if denied := self._check_whitelist(stream_id, kwargs):
            return denied

        if self._busy:
            await self.ctx.send.text(
                "插件正在生成中，请等待生成完成后操作，或使用 /abort 打断",
                stream_id,
            )
            return False, "忙碌中", True

        plugin_id = self._extract_arg(kwargs, r"^/confirm_plugin\s+(.+)$")
        if not plugin_id:
            await self.ctx.send.text("用法: /confirm_plugin <插件ID>", stream_id)
            return False, "缺少插件 ID", True

        staged = self._staged.get(plugin_id)
        if not staged:
            if self._staged:
                ids = ", ".join(list(self._staged.keys())[:5])
                await self.ctx.send.text(
                    f"未找到暂存插件: {plugin_id}\n"
                    f"当前暂存中的插件: {ids}\n"
                    f"使用 /list_staged 查看完整列表",
                    stream_id,
                )
            else:
                await self.ctx.send.text(
                    f"未找到暂存插件: {plugin_id}\n暂存区为空，使用 /list_staged 确认",
                    stream_id,
                )
            return False, "未找到暂存插件", True

        output_base = Path(self.config.generation.output_base_dir)
        if not output_base.is_absolute():
            output_base = PLUGINS_ROOT / output_base
        output_dir = (output_base / plugin_id).resolve()
        try:
            output_dir.relative_to(PLUGINS_ROOT)
        except ValueError:
            await self.ctx.send.text(
                f"❌ 安全拒绝: 输出目录 {output_dir} 不在插件根目录下",
                stream_id,
            )
            return False, "输出路径越界", True
        if output_dir.exists():
            await self.ctx.send.text(
                f"插件 {plugin_id} 已存在于 {output_dir}\n"
                f"如需覆盖请先手动删除该目录，或使用 /reject_plugin {plugin_id} 放弃暂存",
                stream_id,
            )
            return False, "目标目录已存在", True

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            for filename, content in staged["files"].items():
                (output_dir / filename).write_text(content, encoding="utf-8")
        except OSError as e:
            await self.ctx.send.text(f"写入文件失败: {e}", stream_id)
            return False, "文件写入失败", True

        del self._staged[plugin_id]
        self._rmtree(STAGING_DIR / plugin_id)

        await self.ctx.send.text(
            f"✅ 插件 {plugin_id} 已安装到 {output_dir}\n\n"
            f"请手动启用插件：编辑 config.toml 将 plugin.enabled 设为 true",
            stream_id,
        )
        return True, f"已安装 {plugin_id}", True

    # ── Command: /reject_plugin ────────────────────────────────

    @Command(
        "reject_plugin",
        description="放弃暂存中的插件",
        pattern=r"^/reject_plugin\s+(?P<plugin_id>[^\s]+)$",
    )
    async def handle_reject_plugin(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件自生成器未启用", True
        if denied := await self._check_access(stream_id):
            return denied
        if denied := self._check_whitelist(stream_id, kwargs):
            return denied

        if self._busy:
            self._abort_generation()

        plugin_id = self._extract_arg(kwargs, r"^/reject_plugin\s+(.+)$")
        if not plugin_id:
            await self.ctx.send.text("用法: /reject_plugin <插件ID>", stream_id)
            return False, "缺少插件 ID", True

        staged = self._staged.pop(plugin_id, None)
        self._rmtree(STAGING_DIR / plugin_id)

        if staged:
            await self.ctx.send.text(f"🗑 已放弃插件: {plugin_id}", stream_id)
            return True, f"已放弃 {plugin_id}", True
        else:
            if self._staged:
                ids = ", ".join(list(self._staged.keys())[:5])
                await self.ctx.send.text(
                    f"未找到暂存插件: {plugin_id}\n当前暂存: {ids}",
                    stream_id,
                )
            else:
                await self.ctx.send.text(f"未找到暂存插件: {plugin_id}\n暂存区为空", stream_id)
            return False, "未找到暂存插件", True

    # ── Command: /abort ───────────────────────────────────────

    @Command(
        "abort",
        description="取消当前正在进行的插件生成",
        pattern=r"^/abort$",
    )
    async def handle_abort(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件自生成器未启用", True
        if denied := await self._check_access(stream_id):
            return denied

        if not self._busy:
            await self.ctx.send.text("当前没有正在进行的生成任务", stream_id)
            return True, "无任务", True

        if not self._is_operator(stream_id, kwargs):
            return False, "仅操作员可执行中断操作", True

        self._abort_generation()
        await self.ctx.send.text("已发出取消信号，正在终止当前生成...", stream_id)
        return True, "已取消", True

    # ── Command: /list_staged ──────────────────────────────────

    @Command(
        "list_staged",
        description="查看所有待确认的暂存插件",
        pattern=r"^/list_staged$",
    )
    async def handle_list_staged(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件自生成器未启用", True
        if denied := await self._check_access(stream_id):
            return denied
        if denied := self._check_whitelist(stream_id, kwargs):
            return denied

        del kwargs
        self._cleanup_stale_staging()

        if not self._staged:
            await self.ctx.send.text("当前没有待确认的暂存插件 📭", stream_id)
            return True, "暂存列表为空", True

        forward_nodes: list[dict[str, Any]] = []
        forward_nodes.append({
            "user_id": "0",
            "nickname": "暂存列表",
            "segments": [{"type": "text", "content": f"📋 待确认的暂存插件 ({len(self._staged)} 个):"}],
        })

        now = time.time()
        for pid, info in self._staged.items():
            age_sec = int(now - info["created_at"])
            if age_sec < 60:
                age_str = f"{age_sec}秒前"
            elif age_sec < 3600:
                age_str = f"{age_sec // 60}分钟前"
            else:
                age_str = f"{age_sec // 3600}小时前"

            desc = (info.get("description") or "")[:80]
            files_list = ", ".join(sorted(info.get("files", {}).keys()))
            remaining = max(0, int(STAGING_MAX_AGE_SECONDS - (now - info["created_at"])))
            expiry_str = f"剩余 {remaining // 60} 分钟" if remaining < 3600 else f"剩余 {remaining // 3600} 小时"

            content = (
                f"[{pid}] {desc}\n"
                f"文件: {files_list}\n"
                f"创建: {age_str} | {expiry_str}"
            )
            forward_nodes.append({
                "user_id": "0",
                "nickname": f"插件: {pid}",
                "segments": [{"type": "text", "content": content}],
            })

        await self.ctx.send.forward(forward_nodes, stream_id)
        return True, f"共 {len(self._staged)} 个暂存插件", True

    # ── Command: /view_plugin ──────────────────────────────────

    @Command(
        "view_plugin",
        description="查看暂存中插件的完整代码",
        pattern=r"^/view_plugin\s+(?P<plugin_id>[^\s]+)$",
    )
    async def handle_view_plugin(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件自生成器未启用", True
        if denied := await self._check_access(stream_id):
            return denied
        if denied := self._check_whitelist(stream_id, kwargs):
            return denied

        plugin_id = self._extract_arg(kwargs, r"^/view_plugin\s+(.+)$")
        if not plugin_id:
            await self.ctx.send.text("用法: /view_plugin <插件ID>", stream_id)
            return False, "缺少插件 ID", True

        staged = self._staged.get(plugin_id)
        if not staged:
            await self.ctx.send.text(
                f"未找到暂存插件: {plugin_id}\n使用 /list_staged 查看待确认列表",
                stream_id,
            )
            return False, "未找到暂存插件", True

        files: dict[str, str] = staged.get("files", {})
        forward_nodes: list[dict[str, Any]] = []
        for name in sorted(files.keys()):
            content = files[name]
            lines = content.split("\n")
            total_lines = len(lines)
            forward_nodes.append({
                "user_id": "0",
                "nickname": f"{name} ({total_lines}行)",
                "segments": [{"type": "text", "content": content}],
            })

        await self.ctx.send.text(
            f"📄 {plugin_id} 完整代码 ({len(forward_nodes)} 个文件):",
            stream_id,
        )
        await self.ctx.send.forward(forward_nodes, stream_id)
        return True, f"已发送完整代码", True

    # ── Command: /write_help ───────────────────────────────────

    @Command(
        "write_help",
        description="查看插件自生成器的帮助文档",
        pattern=r"^/write_help$",
    )
    async def handle_help(self, stream_id: str = "", **kwargs: Any):
        del kwargs

        forward_nodes: list[dict[str, Any]] = [
            {
                "user_id": "0",
                "nickname": "生成流程",
                "segments": [{"type": "text", "content": (
                    "── 生成流程 ──\n"
                    "  /write_plugin <描述>       → 启动后台生成任务\n"
                    "    支持 --from <插件ID> 或 参考:<插件ID> 指定参考\n"
                    "  (等待通知完成后)\n"
                    "  /view_plugin <插件ID>      → 查看完整代码\n"
                    "  /confirm_plugin <插件ID>   → 安装到 plugins/generated/\n"
                    "  /reject_plugin <插件ID>    → 放弃"
                )}],
            },
            {
                "user_id": "0",
                "nickname": "修改与修复",
                "segments": [{"type": "text", "content": (
                    "── 修改与修复 ──\n"
                    "  /modify_plugin <ID> <需求> → 修改已生成插件的代码\n"
                    "  /fix_plugin <ID> <错误>    → 提交报错日志自动修复\n"
                    "  /analyze_plugin <ID>       → 分析插件功能和原理\n"
                    "  /analyze_plugin <ID> --check → 附带安全排查"
                )}],
            },
            {
                "user_id": "0",
                "nickname": "管理命令",
                "segments": [{"type": "text", "content": (
                    "── 管理命令 ──\n"
                    "  /list_plugins   → 列出可参考的已有插件\n"
                    "  /list_staged    → 查看所有待确认的暂存插件\n"
                    "  /abort          → 取消当前生成任务\n"
                    "  /write_help     → 显示此帮助"
                )}],
            },
            {
                "user_id": "0",
                "nickname": "常用示例",
                "segments": [{"type": "text", "content": (
                    "── 常用示例 ──\n"
                    "  /write_plugin 帮我写一个/hello命令，回复你好世界\n"
                    "  /modify_plugin user.hello 把回复改成图片形式\n"
                    "  /fix_plugin user.hello 'AttributeError: stream_id not found'\n"
                    "  /analyze_plugin chat-summary-plugin --check"
                )}],
            },
        ]

        await self.ctx.send.text(
            "📖 插件自生成器 v0.2.1 帮助\n让麦麦根据自然语言描述自动生成 MaiBot 插件代码。\n详细配置说明见插件目录下的 README.md",
            stream_id,
        )
        await self.ctx.send.forward(forward_nodes, stream_id)
        return True, "已显示帮助", True

    # ── 修改/修复共用 ──────────────────────────────────────────

    async def _run_modify_background(
        self,
        plugin_id: str,
        instruction: str,
        stream_id: str,
        gen_cfg: GenerationConfig,
        forbidden: list[str],
        max_file_size: int,
        is_fix: bool = False,
    ) -> None:
        try:
            self._enter_busy()
            original = self._read_plugin_from_anywhere(plugin_id)
            if not original:
                await self.ctx.send.text(f"未找到插件: {plugin_id}（不在暂存区也未安装）", stream_id)
                return

            if is_fix:
                prompt = build_fix_error_prompt(
                    original, instruction,
                    catalog_text=self._catalog_text,
                )
            else:
                prompt = build_modify_prompt(
                    original, instruction,
                    catalog_text=self._catalog_text,
                )

            api_cfg = self.config.custom_api
            client = create_llm_client(
                self.ctx,
                custom_api_enabled=api_cfg.enabled,
                custom_api_url=api_cfg.api_url,
                custom_api_key=api_cfg.api_key,
                custom_api_model=api_cfg.model,
            )

            model_name = api_cfg.model if api_cfg.enabled else gen_cfg.model
            result = await client.generate(
                prompt=prompt,
                timeout=gen_cfg.llm_timeout_seconds,
                model=model_name,
            )

            if not result.success:
                await self.ctx.send.text(f"LLM 调用失败: {result.error}", stream_id)
                return

            response_text = result.response
            if not response_text.strip():
                await self.ctx.send.text("LLM 返回空响应", stream_id)
                return

            new_files = parse_llm_response(response_text)
            if not new_files:
                await self.ctx.send.text("无法解析 LLM 响应中的代码", stream_id)
                return

            errors = validate_plugin_files(new_files, forbidden, max_file_size)
            if errors:
                error_text = "\n".join(f"  - {e}" for e in errors[:5])
                await self.ctx.send.text(
                    f"生成的代码未通过安全检查:\n{error_text}\n请再次尝试或简化需求",
                    stream_id,
                )
                return

            new_files = self._inject_watermark(new_files, plugin_id, instruction)
            file_hash = compute_files_hash(new_files)
            staged_at = time.time()

            self._staged[plugin_id] = {
                "files": new_files,
                "created_at": staged_at,
                "description": f"[{'修复' if is_fix else '修改'}] {instruction[:80]}",
                "hash": file_hash,
            }
            self._save_staging_files(plugin_id, new_files, staged_at, f"[{'修复' if is_fix else '修改'}] {instruction[:80]}", file_hash)

            action = "修复" if is_fix else "修改"
            await self.ctx.send.text(
                f"✅ {action}完成，已暂存\n\n"
                f"插件 ID: {plugin_id}\n"
                f"SHA256  : {file_hash}\n"
                f"📌 /confirm_plugin {plugin_id}  安装（覆盖原有）\n"
                f"👁 /view_plugin {plugin_id}  查看完整代码\n"
                f"🗑 /reject_plugin {plugin_id}  放弃",
                stream_id,
            )
        except asyncio.CancelledError:
            await self.ctx.send.text("操作已被取消", stream_id)
        except Exception as e:
            await self.ctx.send.text(f"❌ 操作异常: {e}", stream_id)
        finally:
            self._leave_busy()
            self._generation_task = None

    # ── Command: /modify_plugin ────────────────────────────────

    @Command(
        "modify_plugin",
        description="按需求修改已生成的插件（暂存或已安装均可）",
        pattern=r"^/modify_plugin\s+(?P<plugin_id>[^\s]+)\s+(?P<text>.+)$",
    )
    async def handle_modify_plugin(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件自生成器未启用", True
        if denied := await self._check_access(stream_id):
            return denied
        if denied := self._check_whitelist(stream_id, kwargs):
            return denied
        if self._busy:
            await self.ctx.send.text("插件正在处理中，请稍后再试", stream_id)
            return False, "忙碌中", True

        matched = kwargs.get("matched_groups")
        plugin_id = ""
        instruction = ""
        if isinstance(matched, dict):
            plugin_id = str(matched.get("plugin_id", "")).strip()
            instruction = str(matched.get("text", "")).strip()
        if not plugin_id or not instruction:
            raw = self._extract_arg(kwargs, r"^/modify_plugin\s+([^\s]+)\s+(.+)$") or ""
            parts = raw.split(None, 1)
            if len(parts) >= 2:
                plugin_id = plugin_id or parts[0]
                instruction = instruction or parts[1]
        if not plugin_id or not instruction:
            await self.ctx.send.text(
                "用法: /modify_plugin <插件ID> <修改需求>\n"
                "例如: /modify_plugin user.hello 把回复改成图片形式",
                stream_id,
            )
            return False, "参数不足", True

        if not is_safe_plugin_id(plugin_id):
            await self.ctx.send.text(f"插件 ID 无效: {plugin_id}", stream_id)
            return False, "无效 ID", True

        await self.ctx.send.text(f"🔧 正在修改 {plugin_id}...", stream_id)

        gen_cfg = self.config.generation
        safety_cfg = self.config.safety
        forbidden = [s.strip().lower() for s in safety_cfg.forbidden_imports.split(",") if s.strip()]

        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._run_modify_background(
                plugin_id, instruction, stream_id, gen_cfg,
                forbidden, safety_cfg.max_file_size_bytes,
                is_fix=False,
            )
        )
        self._generation_task = task
        return True, "修改任务已启动，完成后会通知你", True

    # ── Command: /fix_plugin ───────────────────────────────────

    @Command(
        "fix_plugin",
        description="提交报错信息让 LLM 自动修复插件",
        pattern=r"^/fix_plugin\s+(?P<plugin_id>[^\s]+)\s+(?P<text>.+)$",
    )
    async def handle_fix_plugin(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件自生成器未启用", True
        if denied := await self._check_access(stream_id):
            return denied
        if denied := self._check_whitelist(stream_id, kwargs):
            return denied
        if self._busy:
            await self.ctx.send.text("插件正在处理中，请稍后再试", stream_id)
            return False, "忙碌中", True

        matched = kwargs.get("matched_groups")
        plugin_id = ""
        error_desc = ""
        if isinstance(matched, dict):
            plugin_id = str(matched.get("plugin_id", "")).strip()
            error_desc = str(matched.get("text", "")).strip()
        if not plugin_id or not error_desc:
            raw = self._extract_arg(kwargs, r"^/fix_plugin\s+([^\s]+)\s+(.+)$") or ""
            parts = raw.split(None, 1)
            if len(parts) >= 2:
                plugin_id = plugin_id or parts[0]
                error_desc = error_desc or parts[1]
        if not plugin_id or not error_desc:
            await self.ctx.send.text(
                "用法: /fix_plugin <插件ID> <错误描述或日志>\n"
                "例如: /fix_plugin user.hello 'AttributeError on line 45: stream_id not found'",
                stream_id,
            )
            return False, "参数不足", True

        if not is_safe_plugin_id(plugin_id):
            await self.ctx.send.text(f"插件 ID 无效: {plugin_id}", stream_id)
            return False, "无效 ID", True

        await self.ctx.send.text(f"🔧 正在分析错误并修复 {plugin_id}...", stream_id)

        gen_cfg = self.config.generation
        safety_cfg = self.config.safety
        forbidden = [s.strip().lower() for s in safety_cfg.forbidden_imports.split(",") if s.strip()]

        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._run_modify_background(
                plugin_id, error_desc, stream_id, gen_cfg,
                forbidden, safety_cfg.max_file_size_bytes,
                is_fix=True,
            )
        )
        self._generation_task = task
        return True, "修复任务已启动，完成后会通知你", True

    # ── Command: /analyze_plugin ───────────────────────────────

    @Command(
        "analyze_plugin",
        description="分析已有插件的功能和原理，加 --check 可附带安全排查",
        pattern=r"^/analyze_plugin\s+(?P<plugin_id>[^\s]+)(\s+(?P<flag>--check))?$",
    )
    async def handle_analyze_plugin(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件自生成器未启用", True
        if denied := await self._check_access(stream_id):
            return denied
        if denied := self._check_whitelist(stream_id, kwargs):
            return denied

        matched = kwargs.get("matched_groups", {})
        plugin_id = str(matched.get("plugin_id") or "").strip()
        with_check = bool(matched.get("flag"))
        if not plugin_id:
            raw = str(kwargs.get("text", ""))
            m = re.match(r"^/analyze_plugin\s+([^\s]+)(\s+(--check))?$", raw)
            if m:
                plugin_id = m.group(1).strip()
                with_check = bool(m.group(3))

        if not plugin_id:
            await self.ctx.send.text(
                "用法: /analyze_plugin <插件ID> [--check]\n"
                "例如: /analyze_plugin deepseek-v4-pro.self-writing-plugin\n"
                "      /analyze_plugin user.hello --check",
                stream_id,
            )
            return False, "缺少插件 ID", True

        if not is_safe_plugin_id(plugin_id):
            await self.ctx.send.text(f"插件 ID 无效: {plugin_id}", stream_id)
            return False, "无效 ID", True

        files = self._read_plugin_from_anywhere(plugin_id)
        if not files:
            await self.ctx.send.text(
                f"未找到插件: {plugin_id}\n（不在暂存区也未安装在 plugins/generated/ 下）",
                stream_id,
            )
            return False, "未找到插件", True

        await self.ctx.send.text(
            f"🔍 正在分析 {plugin_id} {'并排查安全问题' if with_check else ''}...",
            stream_id,
        )

        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._run_analyze_background(plugin_id, files, stream_id, with_check)
        )
        self._generation_task = task
        return True, "分析任务已启动", True

    async def _run_analyze_background(
        self,
        plugin_id: str,
        files: dict[str, str],
        stream_id: str,
        with_check: bool,
    ) -> None:
        try:
            self._enter_busy()

            prompt = build_analyze_prompt(
                files.get("plugin.py", ""),
                files.get("_manifest.json", "{}"),
                with_debug=with_check,
            )

            gen_cfg = self.config.generation
            api_cfg = self.config.custom_api
            client = create_llm_client(
                self.ctx,
                custom_api_enabled=api_cfg.enabled,
                custom_api_url=api_cfg.api_url,
                custom_api_key=api_cfg.api_key,
                custom_api_model=api_cfg.model,
            )

            model_name = api_cfg.model if api_cfg.enabled else gen_cfg.model
            result = await client.generate(
                prompt=prompt,
                timeout=gen_cfg.llm_timeout_seconds,
                model=model_name,
            )

            if not result.success:
                await self.ctx.send.text(f"LLM 调用失败: {result.error}", stream_id)
                return

            analysis = result.response.strip()
            if not analysis:
                await self.ctx.send.text("LLM 返回空响应", stream_id)
                return

            title = f"🔍 {plugin_id} 分析报告" + ("（含安全排查）" if with_check else "")
            CHUNK = 2000
            node_parts: list[str] = []
            for i in range(0, len(analysis), CHUNK):
                node_parts.append(analysis[i : i + CHUNK])

            forward_nodes: list[dict[str, Any]] = []
            for idx, part in enumerate(node_parts):
                nickname = f"{title} ({idx + 1}/{len(node_parts)})" if len(node_parts) > 1 else title
                forward_nodes.append({
                    "user_id": "0",
                    "nickname": nickname,
                    "segments": [{"type": "text", "content": part}],
                })

            await self.ctx.send.text(title, stream_id)
            await self.ctx.send.forward(forward_nodes, stream_id)

        except asyncio.CancelledError:
            await self.ctx.send.text("操作已被取消", stream_id)
        except Exception as e:
            await self.ctx.send.text(f"❌ 分析异常: {e}", stream_id)
        finally:
            self._leave_busy()
            self._generation_task = None


def create_plugin() -> SelfWritingPlugin:
    return SelfWritingPlugin()
