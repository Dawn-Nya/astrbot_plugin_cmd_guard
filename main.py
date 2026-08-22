"""指令守卫（Command Guard） —— AstrBot 插件

功能：
1. 拦截“以唤醒词开头、但未被注册或被禁用”的指令，阻止其继续触发 LLM 回复；
2. 指令消息（含已注册指令）一律禁止 AstrBot 默认的 LLM 兜底——指令报错、
   回复发送失败、或指令没有产出回复时，都不会再把原消息交给 LLM（节约 token）；
3. 支持指令级白名单（完全放行）与黑名单（始终拦截）。

适用平台：OneBot v11（aiocqhttp）与 QQ 官方机器人（qq_official / qq_official_webhook），
可通过配置 ``enabled_platforms`` 调整。

本插件完全由 DeepSeek-v4-flash 开发。
"""

import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# ---------------------------------------------------------------------------
# 内部命令注册表（跨 AstrBot 版本的“尽力而为”导入；失败时降级为仅保留
# 指令黑白名单与报错抑制功能）
# ---------------------------------------------------------------------------
try:
    from astrbot.core.star.filter.command import CommandFilter as _CommandFilter
    from astrbot.core.star.filter.command_group import (
        CommandGroupFilter as _CommandGroupFilter,
    )
    from astrbot.core.star.star import star_map as _star_map
    from astrbot.core.star.star_handler import (
        EventType as _CoreEventType,
        star_handlers_registry as _handlers_registry,
    )
except Exception:  # pragma: no cover - 旧版本 AstrBot 兼容
    _CommandFilter = None
    _CommandGroupFilter = None
    _star_map = None
    _CoreEventType = None
    _handlers_registry = None

# AstrBot 内置指令（/help、/sid 等）所在的模块路径
_BUILTIN_CMD_MODULE = "astrbot.builtin_stars.builtin_commands.main"
# “看起来像指令”的无前缀英文单词（用于 block_bare_commands 模式）
_BARE_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,31}$")
# 指令注册表缓存的刷新间隔（秒）
_COMMAND_CACHE_TTL = 5.0


@register(
    "astrbot_plugin_cmd_guard",
    "Dawn_Nya",
    "拦截未知/被禁用的指令并阻止其触发 LLM 回复；支持指令白名单与黑名单。",
    "1.1.0",
)
class CommandGuardPlugin(Star):
    """指令守卫插件。"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._cache_time = 0.0
        self._enabled_names: set[str] = set()
        self._disabled_names: set[str] = set()
        self._scan_warned = False
        # 指令注册表是否可用；不可用时降级为只保留指令黑白名单与报错抑制（fail-open）
        self._registry_ok = _handlers_registry is not None and _CoreEventType is not None

    # ------------------------------------------------------------------
    # 配置辅助
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default=None):
        """读取插件配置，缺失时返回默认值。"""
        return self.config.get(key, default)

    def _platform_enabled(self, event: AstrMessageEvent) -> bool:
        """当前平台是否在本插件的生效范围内。"""
        platforms = self._cfg("enabled_platforms", None)
        if not platforms:
            return True
        return event.get_platform_name() in platforms

    def _forbid_llm(self, event: AstrMessageEvent) -> None:
        """禁止 AstrBot 默认的 LLM 兜底请求。

        注意：AstrBot 中 ``event.call_llm = True`` 表示“禁止默认 LLM 请求”，
        ``False`` 表示允许（ProcessStage 中 ``not event.call_llm`` 才放行）。
        """
        event.should_call_llm(True)

    # ------------------------------------------------------------------
    # 指令注册表（哪些指令已注册、是否被禁用）
    # ------------------------------------------------------------------

    def _refresh_commands(self) -> None:
        """从 AstrBot 的 handler 注册表构建“已启用指令名 / 已禁用指令名”集合。

        带 TTL 缓存，避免每条消息都重建。WebUI 中启用/禁用/重命名指令后，
        最多 TTL 秒内生效。
        """
        now = time.time()
        if now - self._cache_time < _COMMAND_CACHE_TTL:
            return
        self._cache_time = now
        enabled: set[str] = set()
        disabled: set[str] = set()
        try:
            if _handlers_registry is None or _CoreEventType is None:
                raise RuntimeError("command registry unavailable")
            # 当用户开启了“禁用自带指令”时，内置指令不视为已注册指令
            disable_builtin = False
            try:
                disable_builtin = bool(
                    self.context.get_config().get("disable_builtin_commands", False)
                )
            except Exception:
                pass
            for handler in _handlers_registry:
                if handler.event_type != _CoreEventType.AdapterMessageEvent:
                    continue
                # 已停用（禁用）插件的 handler 仍留在注册表中，但其指令不会被执行，
                # 因此不视为“已注册指令”
                if _star_map is not None:
                    md = _star_map.get(getattr(handler, "handler_module_path", ""))
                    if md is not None and not getattr(md, "activated", True):
                        continue
                if disable_builtin and getattr(
                    handler, "handler_module_path", ""
                ) == _BUILTIN_CMD_MODULE:
                    continue
                target = (
                    disabled if not getattr(handler, "enabled", True) else enabled
                )
                for f in getattr(handler, "event_filters", None) or []:
                    if isinstance(f, (_CommandFilter, _CommandGroupFilter)):
                        target.update(self._filter_names(f))
        except Exception as e:  # 注册表不可用：降级（fail-open），仅保留指令黑白名单与报错抑制
            if not self._scan_warned:
                logger.error(
                    f"cmd_guard: 无法扫描指令注册表，未知/禁用指令拦截不可用: {e}"
                )
                self._scan_warned = True
            self._registry_ok = False
            enabled, disabled = set(), set()
        self._enabled_names = enabled
        self._disabled_names = disabled

    @staticmethod
    def _filter_names(f) -> list[str]:
        """取出 CommandFilter / CommandGroupFilter 的完整指令名（含别名与父级签名）。"""
        try:
            return list(f.get_complete_command_names() or [])
        except Exception:  # 旧版本 AstrBot 的兜底
            base = getattr(f, "command_name", None) or getattr(f, "group_name", None)
            alias = getattr(f, "alias", None) or set()
            names = [base] if base else []
            names += [str(a) for a in alias if a]
            return names

    @staticmethod
    def _match_command(msg: str, names: set[str]) -> str | None:
        """判断消息是否命中某个指令名，语义与 AstrBot 的 CommandFilter 一致。

        即：消息等于指令名，或以“指令名 + 空格”开头。返回命中的最长指令名。
        """
        best = None
        for name in names:
            name = (name or "").strip()
            if not name:
                continue
            if msg == name or msg.startswith(name + " "):
                if best is None or len(name) > len(best):
                    best = name
        return best

    @staticmethod
    def _undecorate(msg: str, prefixes: list[str]) -> str:
        """去掉消息首词上的指令前缀（如 /foobar -> foobar）。"""
        parts = msg.split(" ", 1)
        token = parts[0]
        for p in prefixes:
            if p and token.startswith(p):
                token = token[len(p):]
                rest = parts[1] if len(parts) > 1 else ""
                return (token + " " + rest).strip()
        return msg

    @staticmethod
    def _original_text(event: AstrMessageEvent) -> str:
        """获取用户发送的原始文本（含唤醒词/前缀，AstrBot 只剥离了 event.message_str）。

        优先从消息链重建并跳过 @ / 引用 等非文本段，失败时回退到
        message_obj.message_str（各适配器填充的原始文本）。
        """
        try:
            from astrbot.api.message_components import At, AtAll, Plain, Reply

            parts = []
            for comp in event.get_messages():
                if isinstance(comp, (At, AtAll, Reply)):
                    continue
                if isinstance(comp, Plain):
                    parts.append(comp.text)
                else:
                    parts.append(" ")
            if parts:
                return re.sub(r"\s+", " ", "".join(parts)).strip()
        except Exception:
            pass
        obj = getattr(event, "message_obj", None)
        raw = getattr(obj, "message_str", None)
        if raw and str(raw).strip():
            return re.sub(r"\s+", " ", str(raw)).strip()
        return ""

    @staticmethod
    def _cmd_in_list(candidate: str, texts: list[str], entries) -> bool:
        """候选指令名（或原始消息文本）是否命中指令列表（黑名单/白名单）。

        支持裸名（活字印刷）、带斜杠（/活字印刷）与完整调用（math add 1 2）三种写法。
        """
        entries = [str(x).strip() for x in (entries or []) if str(x).strip()]
        if not entries:
            return False
        if candidate and candidate in entries:
            return True
        for e in entries:
            for text in texts:
                text = text.strip()
                if text == e or text.startswith(e + " "):
                    return True
        return False

    # ------------------------------------------------------------------
    # 拦截动作
    # ------------------------------------------------------------------

    def _build_block(self, event: AstrMessageEvent, template: str, cmd: str):
        """构造拦截结果：禁止 LLM 兜底；模板非空时返回一条提示消息。

        返回 MessageEventResult 或 None（None 表示静默拦截）。
        """
        self._forbid_llm(event)
        text = ""
        try:
            text = (template or "").replace("{cmd}", str(cmd)).strip()
        except Exception:
            text = ""
        if text:
            return event.plain_result(text)
        return None

    # ------------------------------------------------------------------
    # 消息监听：拦截未知 / 被禁用的指令
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，拦截“以唤醒词开头、但未注册或被禁用”的指令。"""
        if not self._cfg("enable", True):
            return

        if not self._platform_enabled(event):
            return
        # 仅在被唤醒（@ 机器人 / 唤醒词 / 私聊）时拦截，与 AstrBot 触发 LLM 的条件一致
        if not event.is_at_or_wake_command:
            return

        msg = re.sub(r"\s+", " ", event.message_str or "").strip()
        if not msg:
            return
        # 原始文本（含唤醒词/前缀）：用于识别“以指令前缀开头”这一强指令信号。
        # AstrBot 的 WakingCheckStage 会先把唤醒词从 event.message_str 中剥离，
        # 因此默认唤醒前缀为 "/" 时，"/foo" 到这里已变成 "foo"，
        # 必须依赖原始文本才能识别出它原本是 "/foo"。
        original = self._original_text(event)
        orig_first = original.split(" ", 1)[0] if original else ""
        msg_first = msg.split(" ", 1)[0]

        # 可选：若其它插件（如正则监听器）也激活了 handler，则放行
        if self._cfg("pass_when_other_handlers", False):
            mine = self.__class__.__module__
            others = [
                h
                for h in (event.get_extra("activated_handlers", []) or [])
                if getattr(h, "handler_module_path", "") != mine
            ]
            if others:
                return

        prefixes = [p for p in (self._cfg("command_prefixes", ["/"]) or []) if p]
        # 消息是否以指令前缀开头（原始文本或剥离唤醒词后的文本）
        prefixed = any(orig_first.startswith(p) for p in prefixes) or any(
            msg_first.startswith(p) for p in prefixes
        )
        # 去掉前缀后的候选指令名（如 /foobar -> foobar）
        undecorated = self._undecorate(msg, prefixes)
        candidate = undecorated.split(" ", 1)[0].strip()
        texts = [msg, original]

        # 0) 指令黑名单：始终拦截（优先于一切，包括已注册启用的指令）
        if self._cfg("blacklist_commands", None):
            if self._cmd_in_list(candidate, texts, self._cfg("blacklist_commands")):
                result = self._build_block(
                    event,
                    self._cfg("blacklist_reply", "指令 {cmd} 已被禁止。"),
                    candidate or msg_first,
                )
                if result is not None:
                    yield result
                event.stop_event()
                return

        # 0b) 指令白名单：完全放行，不做任何拦截
        if self._cfg("whitelist_commands", None):
            if self._cmd_in_list(candidate, texts, self._cfg("whitelist_commands")):
                return

        # 指令注册表不可用（旧版本 AstrBot）时，无法判断“是否已注册”，
        # 采取 fail-open：跳过注册表相关拦截，仅保留指令黑白名单与报错抑制。
        if not self._registry_ok:
            return

        self._refresh_commands()

        # 1) 命中已启用的指令：放行（指令自身处理），但禁止默认 LLM 兜底——
        #    指令报错、回复发送失败或没有产出回复时，都不再把原消息交给 LLM
        if self._match_command(msg, self._enabled_names):
            self._forbid_llm(event)
            return

        # 2) 命中已注册但被禁用的指令：拦截（对去掉前缀后的文本同样匹配，
        #    以覆盖“自定义唤醒词 + /指令名”的写法）
        if self._cfg("block_disabled_commands", True):
            disabled_cmd = self._match_command(msg, self._disabled_names) or (
                self._match_command(undecorated, self._disabled_names)
                if undecorated != msg
                else None
            )
            if disabled_cmd:
                result = self._build_block(
                    event,
                    self._cfg("disabled_reply", "指令 {cmd} 已被禁用。"),
                    disabled_cmd,
                )
                if result is not None:
                    yield result
                event.stop_event()
                return

        # 3) 未注册指令：仅当“看起来像指令”时拦截
        if not self._cfg("block_unknown_commands", True):
            return
        if not candidate:
            return
        extra = {
            str(x) for x in (self._cfg("extra_command_tokens", []) or []) if str(x)
        }
        looks_like_command = prefixed
        if (
            not looks_like_command
            and self._cfg("block_bare_commands", False)
            and bool(_BARE_TOKEN_RE.match(candidate))
        ):
            looks_like_command = True
        if not looks_like_command and candidate in extra:
            looks_like_command = True
        if looks_like_command:
            result = self._build_block(
                event,
                self._cfg("unknown_reply", "未知指令：{cmd}。发送 /help 查看可用指令。"),
                candidate,
            )
            if result is not None:
                yield result
            event.stop_event()

    # ------------------------------------------------------------------
    # 事件钩子：插件指令报错后阻止 LLM 兜底回复
    # ------------------------------------------------------------------

    @filter.on_plugin_error()
    async def on_plugin_error(
        self,
        event: AstrMessageEvent,
        plugin_name: str,
        handler_name: str,
        error: Exception,
        traceback_text: str,
    ):
        """任何插件指令执行报错后，阻止 AstrBot 将消息继续交给 LLM 兜底回复。"""
        if not self._cfg("block_llm_after_error", True):
            return
        self._forbid_llm(event)
        if self._cfg("silence_error_reply", False):
            # 屏蔽 AstrBot 默认的 “:( 出现异常” 错误回显
            event.stop_event()
        logger.info(
            f"cmd_guard: 插件 {plugin_name} 的 {handler_name} 报错，"
            "已阻止 LLM 兜底回复。"
        )

    async def terminate(self):
        """插件卸载 / 停用时的清理（暂无）。"""
