import base64
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import At, File, Image, Plain, Reply, Video


# ── 同机信标：ingress 把实际端口与 token 写在 ~/.dsh/astrbot-ingress.json ──
# 配置里留空就走它，省掉「复制 token / 猜端口」这一步。容器里的 AstrBot 看不到
# 这个文件（要手填 host.docker.internal），所以配置优先、信标兜底。
BEACON_FILENAME = "astrbot-ingress.json"
# base64 入站的上限：超过它就走共享目录或 URL 入站（两处判定共用）
INBOUND_BASE64_LIMIT = 12 * 1024 * 1024
BEACON_MAX_AGE_SEC = 600  # ingress 每 30 秒刷新一次，10 分钟没动静就当它没了
BEACON_EARLY_TOLERANCE_SEC = 60  # 容忍两边时钟差
_beacon_cache: dict[str, Any] = {"at": 0.0, "data": None}


def _parse_iso_ts(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _beacon_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("DSH_INGRESS_BEACON", "").strip()
    if env:
        paths.append(Path(env))
    for home in (os.path.expanduser("~"), os.environ.get("USERPROFILE", "")):
        if not home:
            continue
        path = Path(home) / ".dsh" / BEACON_FILENAME
        if path not in paths:
            paths.append(path)
    return paths


def read_ingress_beacon(now: float | None = None, max_age: float = BEACON_MAX_AGE_SEC) -> dict[str, Any] | None:
    """读本机 ingress 的信标；坏文件、别人的文件、过期的一律当没有。"""
    now = time.time() if now is None else now
    for path in _beacon_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("kind") != "dsh-astrbot-ingress":
            continue
        url = str(data.get("url") or "").strip().rstrip("/")
        if not url:
            continue
        ts = _parse_iso_ts(data.get("updatedAt"))
        if ts is None or not (-BEACON_EARLY_TOLERANCE_SEC <= now - ts <= max_age):
            continue
        return {
            "url": url,
            "token": str(data.get("token") or "").strip(),
            "version": data.get("version"),
            "path": str(path),
        }
    return None


def _cached_beacon(ttl: float = 60.0) -> dict[str, Any] | None:
    """带缓存的信标读取：每 60 秒最多摸一次磁盘。"""
    now = time.time()
    if now - float(_beacon_cache.get("at") or 0.0) < ttl:
        return _beacon_cache.get("data")
    data = read_ingress_beacon()
    _beacon_cache["at"] = now
    _beacon_cache["data"] = data
    return data


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    out = []
    for item in value:
        s = str(item).strip()
        if s:
            out.append(s)
    return out


@register(
    "astrbot_plugin_dsh",
    "local",
    "把指定会话转发给本机 DeepSeek Harness，不接管日常聊天",
    "0.3.13",
)
class DshBridgePlugin(Star):
    # DSH 出站文本的隐藏标记（两个零宽空格）：QQ 里看不见，人格回复不会带。
    # 引用续聊靠它做精确判定，识别不到时再退回归一化指纹。
    OUTBOUND_TAG = "\u200b\u200b"
    # URL 入站选中的 base 缓存多久（含失败结果，避免每条消息都扫一遍候选）
    _INBOUND_URL_PICK_TTL = 600.0
    # ingress 地址探测结果的缓存时长
    _INGRESS_PICK_TTL = 300.0

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._quote_ids: set[str] = set()
        self._quote_texts: list[str] = []
        # 本轮已发出的消息条数（按会话计）：passive-first 用它决定何时转主动
        self._turn_sends: dict[str, int] = {}
        # umo -> (DSH 侧工作区, 取到的时间)；同机入站暂存目录自动兜底要用
        self._dsh_cwd: dict[str, tuple[str, float]] = {}
        # 撞过 40034105 就置位：本进程内不再尝试主动消息
        self._proactive_denied = False
        # 信标只在第一次用上时打一条日志，别刷屏
        self._beacon_logged = False
        # (时间, 选中的 URL 入站 base)：None 表示「探测过、都不通」
        self._inbound_url_pick: tuple[float, str | None] = (0.0, None)
        # (时间, 探测到的 ingress 地址)：没配 ingress_url 且无信标时用
        self._ingress_pick: tuple[float, str | None] = (0.0, None)
        # 候选都不通时，在聊天里提醒一次就够
        self._url_hint_needed = False
        self._url_hint_shown = False
        self._quote_file = Path("/AstrBot/data/plugin_data/astrbot_plugin_dsh/quoted_ids.json")
        if not Path("/AstrBot/data").exists():
            self._quote_file = Path("data/plugin_data/astrbot_plugin_dsh/quoted_ids.json")
        self._load_quote_ids()
        self._install_botpy_raw_payload_shim()

    def _install_botpy_raw_payload_shim(self) -> None:
        """把官方入站消息的原始 payload 留在消息对象上。

        botpy 只解析它认识的字段：官方下发的 `msg_elements`（**被引消息的正文**）
        会被它丢掉（`message_reference` 官方压根不下发，botpy 还会凭空造个
        `{'message_id': None}` 出来，很容易看走眼）。

        两个坑：
        1. **不能替换 `botpy.connection.GroupMessage`** —— AstrBot 自己也子类化了
           `botpy.message.GroupMessage`（`PatchedGroupMessage`），并在
           `ConnectionState.parse_group_message_create` 里直接构造那个子类，
           绕过了 connection 里的名字。所以必须**就地包 `botpy.message.*.__init__`**，
           这样子类调 `super().__init__()` 时也会经过我们。
        2. botpy 的消息类声明了 `__slots__`，但 AstrBot 的子类没声明，
           所以实例有 `__dict__`，能挂 `raw_payload`。
        """
        try:
            import botpy.message as _bm
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dsh] botpy shim skipped: %s", exc)
            return

        for name in ("GroupMessage", "C2CMessage"):
            cls = getattr(_bm, name, None)
            if cls is None or getattr(cls, "_dsh_raw_ok", False):
                continue
            original = cls.__init__

            def patched(inner_self, *args, _orig=original, **kwargs):
                data = kwargs.get("data")
                if data is None and args:
                    data = args[-1]
                _orig(inner_self, *args, **kwargs)
                if isinstance(data, dict):
                    try:
                        inner_self.raw_payload = data
                    except Exception:  # noqa: BLE001 - 挂不上就算了，别影响收消息
                        pass

            cls.__init__ = patched
            cls._dsh_raw_ok = True
            logger.info("[dsh] botpy %s.__init__ 已包上 raw_payload", name)

    def _iter_media_segments(self, event: AstrMessageEvent):
        chain = list(getattr(getattr(event, "message_obj", None), "message", None) or [])
        for seg in chain:
            yield seg
            if isinstance(seg, Reply):
                for inner in getattr(seg, "chain", None) or []:
                    yield inner
        # 官方通道没有 Reply 组件：被引消息的附件在原始 payload 的 msg_elements 里
        try:
            for seg in self._official_quoted_media_segments(event):
                yield seg
        except Exception as exc:  # noqa: BLE001 - 拿不到就算了，别影响正常收消息
            logger.debug("quoted media skipped: %s", exc)

    def _has_inbound_media(self, event: AstrMessageEvent) -> bool:
        return any(isinstance(seg, (Image, File, Video)) for seg in self._iter_media_segments(event))

    def _is_bot_mention(self, event: AstrMessageEvent) -> bool:
        """这条消息是否 @ 了机器人（官方适配器对 @ 场景会补一个指向 self_id 的 At）。"""
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:  # noqa: BLE001
            self_id = ""
        chain = list(getattr(getattr(event, "message_obj", None), "message", None) or [])
        for seg in chain:
            if not isinstance(seg, At):
                continue
            qq = str(getattr(seg, "qq", "") or "")
            if not qq:
                continue
            if not self_id or qq == self_id or qq == "qq_official":
                return True
        return False

    def _inbound_share_dir(self, umo: str = "") -> Path | None:
        """AstrBot 能写、且 DSH 也看得到的入站暂存目录（拿不到就返回 None）。

        1. 配了 `inbound_share_dir` 就用它（分容器的正确做法）；
        2. 没配 + **AstrBot 不在容器里**（与 DSH 同一个文件系统）时自动兜底成
           `<DSH 工作区>/.dsh-inbox` —— 工作区由 `/binding` 的 `cwd` 得到。
           这时 `inbound_dsh_prefix` 也不用填：两边看到的是同一个路径。
        """
        raw = str(self._cfg("inbound_share_dir", "") or "").strip()
        if not raw:
            if self._in_container():
                return None
            cwd = self._dsh_side_cwd(umo)
            if not cwd:
                return None
            raw = str(Path(cwd) / ".dsh-inbox")
        path = Path(raw)
        try:
            if path.is_dir():
                return path
            # 父目录（如 /mnt/d）是挂载点，它不在就别凭空造
            if not path.parent.is_dir():
                logger.warning("inbound_share_dir 的父目录不存在（挂载没配？）：%s", raw)
                return None
            path.mkdir(exist_ok=True)
            return path
        except OSError as exc:
            logger.warning("inbound_share_dir 不可用: %s", exc)
            return None

    def _dsh_side_path(self, dest: Path) -> str:
        prefix = str(self._cfg("inbound_dsh_prefix", "") or "").strip()
        return str(Path(prefix) / dest.name) if prefix else str(dest)

    def _stage_inbound(self, src: Path, name: str, umo: str = "") -> tuple[str, Path] | None:
        """把入站文件拷到共享目录。

        返回 (DSH 侧路径, AstrBot 侧本地路径) 供发完清理；拿不到共享目录时返回 None。
        """
        share = self._inbound_share_dir(umo)
        if share is None:
            return None
        dest = share / f"{int(time.time() * 1000)}-{name}"
        try:
            shutil.copy2(src, dest)
        except OSError as exc:
            logger.warning("拷到 inbound_share_dir 失败: %s", exc)
            return None
        return self._dsh_side_path(dest), dest

    async def _download_to_share(self, url: str, name: str, umo: str = "") -> tuple[str, Path] | None:
        """URL 附件直接下到共享目录，避免先落内存再 base64。"""
        share = self._inbound_share_dir(umo)
        if share is None:
            return None
        dest = share / f"{int(time.time() * 1000)}-{name}"
        try:
            async with httpx.AsyncClient(timeout=120.0, trust_env=False, follow_redirects=True) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with dest.open("wb") as fh:
                        async for chunk in resp.aiter_bytes():
                            fh.write(chunk)
        except Exception as exc:
            logger.warning("下载附件到共享目录失败: %s", exc)
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        return self._dsh_side_path(dest), dest

    def _inbound_url_base(self) -> str:
        """DSH 能访问到的 AstrBot 基址（如 `http://127.0.0.1:6185`）；留空 = 走候选探测。

        注意不能用 AstrBot 主配置里的 `callback_api_base`：那是给协议端看的（Docker 里常是
        `http://astrbot:6185`），宿主机上的 DSH 解析不了。
        """
        return str(self._cfg("inbound_url_base", "") or "").strip().rstrip("/")

    def _inbound_url_limit(self) -> int:
        try:
            mb = float(self._cfg("inbound_url_max_mb", 200) or 200)
        except (TypeError, ValueError):
            mb = 200.0
        return int(max(1.0, mb) * 1024 * 1024)

    def _dashboard_port(self) -> int | None:
        """AstrBot 本体 dashboard 的端口（URL 入站最可能用的候选）。"""
        try:
            from astrbot.core import astrbot_config

            raw = (astrbot_config.get("dashboard") or {}).get("port")
            port = int(raw)
            return port if 0 < port < 65536 else None
        except Exception:  # noqa: BLE001
            return None

    def _candidate_bases(self) -> list[str]:
        """URL 入站的候选地址，按优先级：显式配置 > 本机 dashboard 端口 > 候选列表。

        Docker 里 AstrBot 看不到「宿主把 6185 映射成了哪个端口」，所以后两个候选必须由
        **ingress 侧探测**才能确认（见 `_resolve_inbound_base`）。
        """
        explicit = self._inbound_url_base()
        if explicit:
            return [explicit]
        out: list[str] = []
        port = self._dashboard_port()
        if port:
            out.append(f"http://127.0.0.1:{port}")
        for item in _as_str_list(self._cfg("inbound_url_candidates", [])):
            text = item.strip().rstrip("/")
            if not text:
                continue
            if text.isdigit():
                text = f"http://127.0.0.1:{text}"
            elif not text.startswith(("http://", "https://")):
                text = f"http://{text}"
            if text not in out:
                out.append(text)
        return out

    async def _probe_via_ingress(self, probe_url: str) -> dict:
        """请 ingress 去取一次 probe_url，返回它的判定（ok/status/bytes/error/ms）。

        ⚠️ 必须用 **AsyncClient**：这个方法跑在 AstrBot 的事件循环里，而 ingress 探测要取的
        `/api/file/<token>` 正是**同一个进程的 dashboard** 在提供 —— 用同步 client 会阻塞循环，
        变成「我等自己」的死锁，探测必然超时（2026-09-12 实测踩到：真 token 探测稳稳 5 秒超时）。
        """
        url, bearer, _ = self._ingress_target()
        try:
            async with httpx.AsyncClient(timeout=12.0, trust_env=False) as client:
                r = await client.get(
                    f"{url}/probe-url",
                    params={"url": probe_url},
                    headers={"Authorization": f"Bearer {bearer}"},
                )
                if r.status_code != 200:
                    return {
                        "ok": False,
                        "stage": "ingress",
                        "error": f"/probe-url HTTP {r.status_code}: {r.text[:120]}",
                    }
                return r.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "stage": "ingress", "error": str(exc)}

    async def _probe_inbound_base(self, base: str, local: str) -> bool:
        """让 ingress 用一次性 token 试着取一次：能不能取到，才代表这个 base 真的可用。"""
        try:
            # 每次 register_file 都是一次性 token —— 探测用的和真传的必须分开
            from astrbot.core import file_token_service

            token = await file_token_service.register_file(str(local))
        except Exception as exc:  # noqa: BLE001
            logger.warning("AstrBot 文件服务不可用（%s），回退共享目录 / base64", exc)
            return False
        real = await self._probe_via_ingress(f"{base}/api/file/{token}")
        if real.get("ok"):
            return True
        # 再用一个假 token 探一次，把「宿主不可达」和「token 不被接受」分开
        bogus = await self._probe_via_ingress(f"{base}/api/file/00000000-0000-0000-0000-000000000000")
        logger.info(
            "[dsh] 候选 %s 探测未通过：真 token -> status=%s bytes=%s ms=%s err=%s；假 token -> status=%s err=%s",
            base,
            real.get("status"),
            real.get("bytes"),
            real.get("ms"),
            real.get("error") or "-",
            bogus.get("status"),
            bogus.get("error") or "-",
        )
        return False

    async def _resolve_inbound_base(self, local: str) -> str | None:
        """挑一个 DSH 真能访问到的 base；结果（含失败）缓存 `_INBOUND_URL_PICK_TTL` 秒。"""
        now = time.time()
        picked_at, picked = self._inbound_url_pick
        if picked_at and now - picked_at < self._INBOUND_URL_PICK_TTL:
            return picked
        for base in self._candidate_bases():
            if await self._probe_inbound_base(base, local):
                logger.info("[dsh] URL 入站选中 %s（探测通过）", base)
                self._inbound_url_pick = (now, base)
                return base
            logger.info("[dsh] URL 入站候选不可达：%s", base)
        self._inbound_url_pick = (now, None)
        self._url_hint_needed = True
        return None

    async def _maybe_url_entry(self, local: str, name: str, kind: str) -> dict | None:
        """把本地附件登记成一次性 URL，交给 DSH 自己下载。

        `inbound_url_mode`：`auto`（默认）= 只对超过 base64 上限的附件走 URL；
        `always` = 所有附件都走 URL（完全不依赖共享目录）；`off` = 关闭。
        任何一步失败/未配置都返回 None，由调用方回退原有的共享目录 / base64。
        """
        if not local:
            return None
        if not self._candidate_bases():
            return None
        mode = str(self._cfg("inbound_url_mode", "auto") or "auto").strip().lower()
        if mode in {"off", "never", "false", "0", "no", "none"}:
            return None
        try:
            size = Path(local).stat().st_size
        except OSError:
            return None
        if mode != "always" and size <= INBOUND_BASE64_LIMIT:
            return None
        if size > self._inbound_url_limit():
            return None  # 太大：交给后面的 too_big 提示，别让 DSH 白下一遍
        base = await self._resolve_inbound_base(local)
        if not base:
            return None
        try:
            from astrbot.core import file_token_service

            token = await file_token_service.register_file(str(local))
        except Exception as exc:  # noqa: BLE001
            logger.warning("登记入站 URL 失败（%s），回退共享目录 / base64", exc)
            return None
        return {"kind": kind, "name": name, "size": size, "url": f"{base}/api/file/{token}"}

    async def _collect_files(self, event: AstrMessageEvent) -> list[dict]:
        out: list[dict] = []
        umo = self._convo_key(event)
        for seg in self._iter_media_segments(event):
            try:
                if isinstance(seg, Video):
                    local = await seg.convert_to_file_path()
                    name = Path(local).name or "video.mp4"
                    entry = await self._maybe_url_entry(local, name, "video")
                    if entry:
                        out.append(entry)
                        continue
                    staged = self._stage_inbound(Path(local), name, umo)
                    if staged:
                        out.append({"kind": "video", "name": name, "path": staged[0], "_staged": staged[1]})
                        continue
                    data = Path(local).read_bytes()
                    if len(data) > INBOUND_BASE64_LIMIT:
                        out.append({"kind": "video", "name": name, "too_big": True})
                        continue
                    out.append({
                        "kind": "video",
                        "name": name,
                        "data": base64.b64encode(data).decode("ascii"),
                    })
                elif isinstance(seg, Image):
                    local = await seg.convert_to_file_path()
                    name = Path(local).name or "image.png"
                    entry = await self._maybe_url_entry(local, name, "image")
                    if entry:
                        out.append(entry)
                        continue
                    staged = self._stage_inbound(Path(local), name, umo)
                    if staged:
                        out.append({"kind": "image", "name": name, "path": staged[0], "_staged": staged[1]})
                        continue
                    data = Path(local).read_bytes()
                    if len(data) > INBOUND_BASE64_LIMIT:
                        out.append({"kind": "image", "name": name, "too_big": True})
                        continue
                    out.append({
                        "kind": "image",
                        "name": name,
                        "data": base64.b64encode(data).decode("ascii"),
                    })
                elif isinstance(seg, File):
                    local = ""
                    try:
                        local = await seg.get_file()
                    except Exception as exc:
                        logger.warning("get_file failed: %s", exc)
                    if not local:
                        name = str(getattr(seg, "name", "") or "文件")
                        out.append({
                            "kind": "file",
                            "name": name,
                            "missing": True,
                        })
                        continue
                    name = str(seg.name or Path(local).name or "file")
                    is_url = local.startswith("http://") or local.startswith("https://")
                    if not is_url:
                        # 本地文件：大文件可以登记成一次性 URL 让 DSH 自己来取
                        entry = await self._maybe_url_entry(local, name, "file")
                        if entry:
                            out.append(entry)
                            continue
                    staged = (
                        await self._download_to_share(local, name, umo) if is_url
                        else self._stage_inbound(Path(local), name, umo)
                    )
                    if staged:
                        out.append({"kind": "file", "name": name, "path": staged[0], "_staged": staged[1]})
                        continue
                    if is_url:
                        async with httpx.AsyncClient(timeout=30.0, trust_env=False, follow_redirects=True) as client:
                            resp = await client.get(local)
                            resp.raise_for_status()
                            data = resp.content
                    else:
                        data = Path(local).read_bytes()
                    if len(data) > INBOUND_BASE64_LIMIT:
                        out.append({"kind": "file", "name": name, "too_big": True})
                        continue
                    out.append({
                        "kind": "file",
                        "name": name,
                        "data": base64.b64encode(data).decode("ascii"),
                    })
            except Exception as exc:
                logger.warning("collect dsh attachment failed: %s", exc)
        return out

    def _looks_like_approval(self, text: str) -> bool:
        """能不能接住审批/提问的回复。

        纯编号也要转发：提问的选项是连号编号（`3`、`1,3`），不认就被人格当闲聊答了。
        真正有没有未决提问由 ingress 判断，这里只管「别把它漏给人格」。
        """
        t = text.strip().lstrip("/").strip().lower()
        if t in {"1", "2", "yes", "y", "no", "n", "批准", "同意", "拒绝", "不同意",
                 "取消", "算了", "跳过", "skip", "cancel"}:
            return True
        return bool(re.fullmatch(r"\d+(?:[\s,，、]+\d+)*", t))

    def _umo_is_bound(self, umo: str) -> bool:
        """这个会话是否已绑定 DSH。

        顺手把网关返回的 `cwd`（DSH 侧的工作区）缓存下来 —— 同机部署时
        `inbound_share_dir` 可以自动兜底成 `<工作区>/.dsh-inbox`，见 `_inbound_share_dir`。
        """
        if not umo:
            return False
        url, token, _ = self._ingress_target()
        if not token:
            return False
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                r = client.get(f"{url}/binding", params={"umo": umo}, headers={"Authorization": f"Bearer {token}"})
                if r.status_code == 200:
                    payload = r.json()
                    cwd = str(payload.get("cwd") or "").strip()
                    if cwd:
                        self._dsh_cwd[umo] = (cwd, time.time())
                    return bool(payload.get("bound"))
        except Exception:
            return False
        return False

    def _dsh_side_cwd(self, umo: str) -> str:
        """这个会话在 DSH 侧的工作区。

        缓存 5 分钟：`/dsh ws` 会换工作区，过期就重新问一次网关。
        """
        if not umo:
            return ""
        entry = self._dsh_cwd.get(umo)
        if entry and time.time() - entry[1] < 300:
            return entry[0]
        self._umo_is_bound(umo)  # 顺便刷新缓存
        entry = self._dsh_cwd.get(umo)
        return entry[0] if entry else ""

    def _load_quote_ids(self) -> None:
        try:
            if self._quote_file.exists():
                data = json.loads(self._quote_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._quote_ids = {str(x) for x in data[-400:]}
                elif isinstance(data, dict):
                    ids = data.get("ids") or []
                    texts = data.get("texts") or []
                    if isinstance(ids, list):
                        self._quote_ids = {str(x) for x in ids[-400:]}
                    if isinstance(texts, list):
                        self._quote_texts = [str(x) for x in texts[-80:] if str(x).strip()]
        except Exception:
            self._quote_ids = set()
            self._quote_texts = []

    def _save_quote_ids(self) -> None:
        try:
            self._quote_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ids": list(self._quote_ids)[-400:],
                "texts": self._quote_texts[-80:],
            }
            self._quote_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self._quote_ids = set(payload["ids"])
            self._quote_texts = list(payload["texts"])
        except Exception as exc:
            logger.warning("save dsh quote ids failed: %s", exc)

    def _fingerprint_text(self, text: str) -> str:
        """归一化指纹：去掉零宽字符、markdown 符号与全部空白，便于跨客户端比较。"""
        t = str(text or "")
        for ch in ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"):
            t = t.replace(ch, "")
        t = re.sub(r"[`*_~>#|\[\]()!]", "", t)
        t = re.sub(r"\s+", "", t)
        return t[:200]

    def _remember_outbound(self, message_id: Any = None, text: str = "") -> None:
        changed = False
        mid = str(message_id or "").strip()
        if mid and mid not in self._quote_ids:
            self._quote_ids.add(mid)
            if len(self._quote_ids) > 500:
                self._quote_ids = set(list(self._quote_ids)[-400:])
            changed = True
        fp = self._fingerprint_text(text)
        if fp and fp not in self._quote_texts:
            self._quote_texts.append(fp)
            self._quote_texts = self._quote_texts[-80:]
            changed = True
        if changed:
            self._save_quote_ids()

    def _quoted_dsh_reply(self, event: AstrMessageEvent):
        chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        for seg in chain:
            if isinstance(seg, Reply):
                return seg
        return None

    def _official_raw_payload(self, event: AstrMessageEvent):
        """官方入站消息的原始 payload（由 `_install_botpy_raw_payload_shim` 挂上）。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        payload = getattr(raw, "raw_payload", None)
        return payload if isinstance(payload, dict) else None

    def _official_quoted_media_segments(self, event: AstrMessageEvent) -> list:
        """官方通道「被引消息」里的图/文件。

        实测：AstrBot 只会把**本条消息**的附件变成 `Image` 段，被引消息的附件它完全不碰
        （官方适配器不建 `Reply` 组件）；但官方把被引附件原样放在原始 payload 的
        `msg_elements[].attachments[]` 里，**带可直接下载的 url**：

            msg_elements = [{"attachments": [{"content_type": "image/png",
                              "filename": "…jpg", "size": 47001,
                              "url": "https://multimedia.nt.qq.com.cn/download?…"}], …}]

        这里转成 AstrBot 的组件，交给上面统一的媒体收集流程（`_collect_files`）。
        """
        payload = self._official_raw_payload(event)
        if not payload:
            return []
        segments: list = []
        for element in payload.get("msg_elements") or []:
            if not isinstance(element, dict):
                continue
            for att in element.get("attachments") or []:
                if not isinstance(att, dict):
                    continue
                url = str(att.get("url") or "").strip()
                if not url:
                    continue
                content_type = str(att.get("content_type") or "").lower()
                name = str(att.get("filename") or "").strip()
                if content_type.startswith("image"):
                    segments.append(Image.fromURL(url))
                elif content_type.startswith("video") and hasattr(Video, "fromURL"):
                    segments.append(Video.fromURL(url))
                else:
                    # 其余一律按文件发；File 的 get_file() 支持直接给 URL
                    segments.append(File(name=name or "file.bin", file=url))
        return segments

    def _official_quoted_text(self, event: AstrMessageEvent) -> str:
        """官方通道的被引正文：在原始 payload 的 `msg_elements[0].content` 里。

        AstrBot 不会把它做成 `Reply` 组件（botpy 直接丢了），所以要自己从
        raw_payload 里捡 —— raw_payload 由 `_install_botpy_raw_payload_shim` 挂上。
        """
        payload = self._official_raw_payload(event)
        if not payload:
            return ""
        elements = payload.get("msg_elements")
        if not isinstance(elements, list) or not elements:
            return ""
        parts = [
            str(el.get("content") or "")
            for el in elements
            if isinstance(el, dict)
        ]
        return "\n".join(p for p in parts if p).strip()

    def _quoted_is_dsh_content(self, reply) -> bool:
        rid = str(getattr(reply, "id", "") or "").strip()
        if rid and rid in self._quote_ids:
            return True
        full = str(getattr(reply, "message_str", None) or getattr(reply, "text", None) or "")
        if not full.strip():
            chain = getattr(reply, "chain", None) or []
            parts = []
            for seg in chain:
                parts.append(str(getattr(seg, "text", None) or getattr(seg, "message_str", None) or ""))
            full = "\n".join(parts)
        # ① 隐藏标记：DSH 出站消息开头带零宽空格，人格回复不会带。
        if self.OUTBOUND_TAG in full or "\u200b" in full:
            return True
        # ② 归一化指纹：精确相等 → 前缀 40 字 → 后缀 30 字（容忍平台截断/重排）。
        quoted = self._fingerprint_text(full)
        if not quoted:
            return False
        head = quoted[:40]
        tail = quoted[-30:]
        for stored in self._quote_texts:
            if stored == quoted:
                return True
            if len(head) >= 12 and stored.startswith(head):
                return True
            if len(tail) >= 12 and stored.endswith(tail):
                return True
            if len(stored) >= 12 and quoted.startswith(stored[:40]):
                return True
        # ③ 固定标记词（归一化后比较）
        markers = ("本回合结束", "已交给DeepSeekHarness", "DSH入站正常", "操作权限确认")
        return any(m in quoted for m in markers)

    def _is_dsh_quote(self, event: AstrMessageEvent) -> bool:
        if not bool(self._cfg("quote_continue", True)):
            return False
        reply = self._quoted_dsh_reply(event)
        if reply is not None:
            return self._quoted_is_dsh_content(reply)
        # 官方 Bot：AstrBot 侧没有 Reply 组件，被引正文只在原始 payload 里
        text = self._official_quoted_text(event)
        if not text:
            return False
        return self._quoted_is_dsh_content(SimpleNamespace(id="", message_str=text, chain=[]))

    def _is_official_qq(self, event: AstrMessageEvent) -> bool:
        name = str(event.get_platform_name() or "").lower()
        return name in {"qqofficial", "qq_official", "qqbot"}

    def _soften_official_markdown(self, text: str) -> str:
        lines = text.split("\n")
        out: list[str] = []
        in_code = False
        in_table = False
        table_buf: list[str] = []

        def flush_table():
            nonlocal table_buf
            if not table_buf:
                return
            rows = []
            for raw in table_buf:
                cells = [c.strip() for c in raw.strip().strip("|").split("|")]
                if cells and all(set(c) <= set("-: ") and c for c in cells):
                    continue
                rows.append(" · ".join(cells))
            out.extend(rows)
            table_buf = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                flush_table()
                in_code = not in_code
                in_table = False
                out.append(line)
                continue
            if in_code:
                out.append(line)
                continue
            looks_table = stripped.startswith("|") and stripped.count("|") >= 2
            if looks_table:
                in_table = True
                table_buf.append(line)
                continue
            if in_table:
                flush_table()
                in_table = False
            out.append(line)
        flush_table()
        text = "\n".join(out)
        text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1 \2", text)
        return text

    def _tag_outbound(self, text: str) -> str:
        t = str(text or "")
        if not t or t.startswith(self.OUTBOUND_TAG):
            return t
        return self.OUTBOUND_TAG + t

    def _reply_result(self, event: AstrMessageEvent, text: str):
        body = self._tag_outbound(text)
        result = event.plain_result(body)
        if self._is_official_qq(event) and hasattr(result, "use_markdown"):
            result.use_markdown(True)
            try:
                result.chain[0].text = self._soften_official_markdown(body)  # type: ignore[attr-defined]
            except Exception:
                pass
        return result

    def _ingress_candidates(self) -> list[str]:
        """没配 `ingress_url`、也没有信标时（典型：Docker 里读不到宿主机 home）的候选地址。"""
        out: list[str] = ["http://host.docker.internal:3188", "http://127.0.0.1:3188"]
        for item in _as_str_list(self._cfg("ingress_url_candidates", [])):
            text = item.strip().rstrip("/")
            if not text:
                continue
            if text.isdigit():
                text = f"http://127.0.0.1:{text}"
            elif not text.startswith(("http://", "https://")):
                text = f"http://{text}"
            if text not in out:
                out.append(text)
        return out

    async def _ensure_ingress_pick(self) -> None:
        """探测哪个 ingress 地址能用——`/health` 不需要 token，正好用来试探。"""
        if str(self._cfg("ingress_url", "") or "").strip():
            return
        now = time.time()
        picked_at, _ = self._ingress_pick
        if picked_at and now - picked_at < self._INGRESS_PICK_TTL:
            return
        for cand in self._ingress_candidates():
            try:
                async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                    r = await client.get(f"{cand}/health")
                    if r.status_code == 200 and (r.json() or {}).get("plugin"):
                        self._ingress_pick = (now, cand)
                        logger.info("[dsh] ingress 地址选中 %s（/health 探测通过）", cand)
                        return
            except Exception:  # noqa: BLE001
                continue
        self._ingress_pick = (now, None)
        logger.warning("[dsh] 没探测到可用的 ingress 地址，候选：%s", "、".join(self._ingress_candidates()))

    def _ingress_target(self) -> tuple[str, str, str]:
        """(base_url, token, 来源)。留空时依次用：信标 → 候选探测结果 → 候选第一项。"""
        url = str(self._cfg("ingress_url", "") or "").strip().rstrip("/")
        token = str(self._cfg("token", "") or "").strip()
        source = "config"
        if not url or not token:
            beacon = _cached_beacon()
            if beacon:
                if not url:
                    url = beacon["url"]
                if not token and beacon.get("token"):
                    token = beacon["token"]
                source = "beacon"
                if not self._beacon_logged:
                    self._beacon_logged = True
                    logger.info(
                        "[dsh] 用同机信标 %s：%s（ingress %s）",
                        beacon["path"],
                        url,
                        beacon.get("version") or "?",
                    )
        if not url:
            _, picked = self._ingress_pick
            if picked:
                url, source = picked, "probe"
            else:
                url, source = self._ingress_candidates()[0], "default"
        return url, token, source

    def _cfg(self, key: str, default: Any) -> Any:
        if hasattr(self.config, "get"):
            value = self.config.get(key, default)
            return default if value is None else value
        return getattr(self.config, key, default)

    def _enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    def _progress_spec(self) -> dict[str, Any]:
        """过程显示档位交给 ingress 执行：那边才知道什么是「过程」、什么是「结果」。

        纯静默档不行：AstrBot 这条 SSE 按「读空闲」算超时，
        周期汇报同时充当保活心跳，间隔必须小于 timeout_sec。
        """
        mode = str(self._cfg("progress_mode", "digest") or "digest").strip().lower()
        if mode not in {"digest", "full", "minimal"}:
            mode = "digest"
        try:
            interval = int(float(self._cfg("progress_interval_sec", 60) or 60))
        except (TypeError, ValueError):
            interval = 60
        timeout = float(self._cfg("timeout_sec", 600) or 600)
        cap = int(min(300, max(10, timeout - 30))) if timeout > 40 else 10
        interval = max(10, min(cap, interval))
        return {"mode": mode, "intervalSec": interval}

    def _command(self) -> str:
        raw = str(self._cfg("command", "dsh") or "dsh").strip().lstrip("/")
        return raw or "dsh"

    def _allow_users(self) -> set[str]:
        return set(_as_str_list(self._cfg("allow_users", [])))

    def _allow_groups(self) -> set[str]:
        return set(_as_str_list(self._cfg("allow_groups", [])))

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            admins = set(_as_str_list(getattr(event, "role", None)))
            return event.get_sender_id() in admins

    def _allowed(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "")
        group = str(event.get_group_id() or "")
        if self._is_admin(event):
            return True
        users = self._allow_users()
        groups = self._allow_groups()
        if group:
            return group in groups and (not users or sender in users)
        return sender in users

    def _should_capture(self, event: AstrMessageEvent, text: str) -> tuple[bool, str]:
        if not self._enabled():
            return False, ""
        # 「引用 DSH 的回复 = 续聊」，但**@了别人就不算**：那是在跟那个人说话。
        # 这条判断不能省 —— 「引的是不是 DSH 的正文」只能按内容认，别人把正文复制一遍再被引用
        # 就分不出来了（实测踩过）。
        if self._is_dsh_quote(event) and not self._mentions_someone_else(event):
            return True, (text or "").strip() or ("请查看附件" if self._has_inbound_media(event) else "(继续)")
        # 官方通道：AstrBot 不建 Reply，「引用一张图/文件 + @机器人」也接住
        # （附件由 `_official_quoted_media_segments` 从原始 payload 里取）。
        if self._is_official_qq(event) \
            and self._is_bot_mention(event) \
            and self._has_inbound_media(event) \
            and self._umo_is_bound(self._convo_key(event)):
            return True, (text or "").strip() or "请查看附件"
        if bool(self._cfg("bound_media_passthrough", True)) \
            and self._has_inbound_media(event) \
            and self._umo_is_bound(self._convo_key(event)):
            return True, (text or "").strip() or "请查看附件"
        if not text and not self._has_inbound_media(event):
            return False, ""
        if self._looks_like_approval(text) and self._umo_is_bound(self._convo_key(event)):
            return True, text.strip()
        # 官方群聊：能收到就说明是被 @ 的，已绑定会话里直接当续聊（不必 /dsh 或引用）
        if bool(self._cfg("official_at_continue", True)) \
            and self._is_official_qq(event) \
            and self._is_bot_mention(event) \
            and self._umo_is_bound(self._convo_key(event)):
            return True, text.strip() or "请查看附件"
        cmd = self._command().lower()
        lowered = text.strip()
        body = lowered[1:] if lowered.startswith("/") else lowered
        if body.lower() == cmd or body.lower().startswith(cmd + " "):
            payload = body[len(cmd):].strip()
            if not payload and self._has_inbound_media(event):
                return True, "请查看附件"
            return True, payload or "status"
        if not event.get_group_id() and bool(self._cfg("private_passthrough", False)):
            return True, text.strip()
        return False, ""

    async def _forward(self, event: AstrMessageEvent, text: str) -> AsyncIterator[str]:
        # 没配地址也没信标时，先探一个能用的（Docker 里 host.docker.internal:3188 是常态）
        await self._ensure_ingress_pick()
        url, token, _source = self._ingress_target()
        timeout = float(self._cfg("timeout_sec", 600) or 600)
        if not token:
            yield (
                "DSH 桥没拿到 token，也没找到同机信标（~/.dsh/astrbot-ingress.json）。"
                "同机部署：确认 DSH 里已启用 dsh-astrbot-ingress（它会写这个文件），"
                "插件配置里的 ingress_url / token 留空即可；"
                "容器部署：ingress_url 填 host.docker.internal:3188，token 手动填"
                "宿主机 ~/.dsh/dsh-astrbot-ingress/config.json 里那个。"
            )
            return
        files = await self._collect_files(event)
        missing = [f.get("name") or "文件" for f in files if f.get("missing")]
        files = [f for f in files if not f.get("missing")]
        too_big = [f.get("name") or "文件" for f in files if f.get("too_big")]
        files = [f for f in files if not f.get("too_big")]
        if too_big:
            if self._inbound_url_base():
                url_hint = "，或把 inbound_url_max_mb 调大（URL 入站的上限）"
            elif self._url_hint_needed and not self._url_hint_shown:
                self._url_hint_shown = True
                tried = "、".join(self._candidate_bases()) or "（无候选）"
                url_hint = (
                    f"。URL 入站试过这些地址都不通：{tried}"
                    "（AstrBot 在 Docker 里时，要填宿主机能访问到的那一个，例如 "
                    "`docker port` 查到的映射端口 `http://127.0.0.1:10000`；"
                    "也可以把候选写进 `inbound_url_candidates`）"
                )
            else:
                url_hint = "，或者填 inbound_url_base 走 URL 入站（不用挂共享盘）"
            share_hint = "" if self._cfg("inbound_share_dir", "") else "，或在插件配置里填 inbound_share_dir / inbound_dsh_prefix 走共享目录"
            yield "附件太大没能传过去（超过 12MB）：" + "、".join(too_big) + share_hint + url_hint + "。"
        if missing:
            yield "引用的文件没能下载（群文件/过期链接常见）：" + "、".join(missing) + "。请把文件当聊天附件发出，或同一条消息里带 /dsh。"
        # 共享目录里的暂存副本：ingress 读完这轮就删，别一直堆着
        staged_locals = [f.pop("_staged") for f in files if f.get("_staged")]
        payload = {
            "umo": self._convo_key(event),
            "chatType": "group" if event.get_group_id() else "private",
            "senderId": str(event.get_sender_id() or ""),
            "groupId": str(event.get_group_id() or "") or None,
            "text": text,
            "messageId": str(getattr(event.message_obj, "message_id", "") or ""),
            "files": files,
            "progress": self._progress_spec(),
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        try:
            # trust_env=False：容器 HTTP_PROXY 会把 host.docker.internal 拐去 Clash 然后 502。
            # 超时按「空闲」算，不是整轮总时长：DSH 跑几十分钟也不会被掐断，
            # 只有连心跳都停了这么久才放弃。
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    None,
                    connect=10.0,
                    read=timeout,
                    write=30.0,
                    pool=30.0,
                ),
                trust_env=False,
            ) as client:
                async with client.stream("POST", f"{url}/inbound", headers=headers, json=payload) as resp:
                    if resp.status_code == 401:
                        yield "DSH 拒绝了 token，请核对插件配置与 ingress config.json。"
                        return
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                        yield f"DSH ingress HTTP {resp.status_code}: {body}"
                        return
                    event_name = "message"
                    data_lines: list[str] = []
                    trace_started = time.monotonic()

                    def flush_event():
                        raw = "\n".join(data_lines).strip()
                        data_lines.clear()
                        name = event_name
                        if not raw:
                            return None
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            data = {"text": raw}
                        if not isinstance(data, dict):
                            data = {"text": str(data)}
                        data["_event"] = name
                        return data

                    async for line in resp.aiter_lines():
                        if line.startswith("event:"):
                            event_name = line[6:].strip() or "message"
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                            continue
                        if line == "":
                            flushed = flush_event()
                            event_name = "message"
                            if flushed:
                                self._trace_sse(flushed, trace_started)
                                async for item in self._emit_sse(event, flushed):
                                    yield item
                    leftover = flush_event()
                    if leftover:
                        self._trace_sse(leftover, trace_started)
                        async for item in self._emit_sse(event, leftover):
                            yield item
        except httpx.ConnectError:
            yield f"连不上 DSH ingress（{url}）。确认 dsh web 已启动，Docker 下用 host.docker.internal，同机直连用 127.0.0.1。"
        except httpx.TimeoutException:
            yield f"DSH 超过 {int(timeout)} 秒没有任何输出，已停止等待。任务可能仍在跑，可在 DSH Web 查看该会话。"
        except Exception as exc:  # noqa: BLE001
            logger.error("dsh bridge failed: %s", exc)
            yield f"DSH 桥出错：{exc}"
        finally:
            for staged in staged_locals:
                try:
                    Path(staged).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("清理入站暂存失败 %s: %s", staged, exc)

    def _container_path(self, host_path: str) -> str:
        p = host_path.replace("/", "\\")
        if len(p) >= 3 and p[1] == ":" and p[0].isalpha():
            drive = p[0].lower()
            rest = p[2:].lstrip("\\").replace("\\", "/")
            return f"/mnt/{drive}/{rest}"
        return host_path

    def _in_container(self) -> bool:
        return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()

    def _send_mode(self, event: AstrMessageEvent | None = None) -> str:
        mode = str(self._cfg("send_file_mode", "auto") or "auto").strip().lower()
        if mode in {"direct", "shared"}:
            return mode
        # 官方 Bot 由 AstrBot 自己上传，不经过 NapCat realpath。
        if event is not None and self._is_official_qq(event):
            return "direct"
        if not self._in_container():
            return "direct"
        # 容器里也只在「确实有协议端能读到的目录」时才拷贝；否则直接发原路径，
        # 免得凭空造出一个协议端看不到的目录，最后报 ENOENT。
        return "shared" if (self._protocol_dir() or self._outbox_dir()) else "direct"

    def _ensure_dir(self, raw: str) -> Path | None:
        """只用已存在的目录，或补建最后一级。

        绝不 `mkdir -p` 整条路径：猜出来的路径一旦被凭空创建，
        拷进去的文件协议端根本读不到，最后报一个很难懂的 ENOENT。
        """
        if not raw:
            return None
        path = Path(raw)
        try:
            if path.is_dir():
                return path
            if not path.parent.is_dir():
                return None
            path.mkdir(exist_ok=True)
            return path
        except OSError:
            return None

    def _outbox_dir(self) -> Path | None:
        """AstrBot 侧可写的发件目录。

        顺序：显式配置 → 常见协议端共享盘（仅在挂载真的存在时）。
        `/AstrBot/data/...` 这类 AstrBot 私有目录不用：协议端永远读不到。
        """
        candidates = []
        configured = str(self._cfg("send_outbox_dir", "") or "").strip()
        if configured:
            candidates.append(configured)
        if self._in_container():
            # SnowLuma / NapCat 常见的共享盘；父目录在才认为挂载存在
            candidates.append("/app/snowluma-data/dsh-outbox")
            candidates.append("/app/napcat/data/dsh-outbox")
        seen: set[str] = set()
        for raw in candidates:
            if not raw or raw in seen:
                continue
            seen.add(raw)
            hit = self._ensure_dir(raw)
            if hit is not None:
                return hit
        return None

    def _protocol_dir(self) -> Path | None:
        proto = str(self._cfg("send_protocol_path", "") or "").strip()
        return self._ensure_dir(proto) if proto else None

    def _stage_for_protocol(self, src: Path, file_name: str) -> tuple[str | None, str | None]:
        """Copy bytes to a directory the protocol client can read.

        Prefer writing into send_protocol_path when AstrBot can see that folder.
        Otherwise copy into send_outbox_dir (same volume, different mount) and
        still tell OneBot the protocol-side path.
        """
        proto_raw = str(self._cfg("send_protocol_path", "") or "").strip()
        proto_dir = self._protocol_dir()
        if proto_dir is not None:
            dest = proto_dir / file_name
            if dest.resolve() != src.resolve():
                shutil.copy2(src, dest)
            return str(dest), None
        outbox = self._outbox_dir()
        if outbox is None:
            return None, "未配置可用的 send_outbox_dir / send_protocol_path"
        dest = outbox / file_name
        if dest.resolve() != src.resolve():
            shutil.copy2(src, dest)
        send_path = str(Path(proto_raw) / file_name) if proto_raw else str(dest)
        return send_path, None

    IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
    INLINE_IMAGE_MAX_BYTES = 5 * 1024 * 1024

    def _is_video_delivery(self, file_name: str) -> bool:
        """视频用 Video 段发（QQ 里能直接播），失败再回退成文件卡片。"""
        if not bool(self._cfg("send_inline_videos", True)):
            return False
        return Path(file_name).suffix.lower() in self.VIDEO_SUFFIXES

    def _is_inline_image(self, file_name: str, size: int) -> bool:
        if not bool(self._cfg("send_inline_images", True)):
            return False
        if Path(file_name).suffix.lower() not in self.IMAGE_SUFFIXES:
            return False
        return 0 < size <= self.INLINE_IMAGE_MAX_BYTES

    def _resolve_local_file(self, host_path: str) -> Path | None:
        if not host_path:
            return None
        mapped = Path(self._container_path(host_path))
        if mapped.is_file():
            return mapped
        raw = Path(host_path)
        if raw.is_file():
            return raw
        return None

    def _convo_key(self, event: AstrMessageEvent) -> str:
        """上桥用的会话键 —— 决定「谁和谁共用一条 DSH 会话」。

        `group`（默认）：一个群 / 一个私聊 = 一条会话，群里所有人共用（现状）。
        `user`：群里**每人一条**（私聊本来就按人）；想共用的人各自
        `/dsh use <同一个短id>` 手动组队即可。

        键只在**插件侧**改写：ingress 那边 umo 纯粹是索引键（它从不用 umo 发消息，
        实际发送走 AstrBot 的事件），所以会话/回合/审批/提问/进度会自动跟着隔离。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo:
            # 有些适配器 / 自建调用不带 umo：退到 session_id，再退到 sender_id，
            # 至少别把所有会话都挤进同一个空键里（跟同类插件学的兜底链）。
            umo = str(getattr(event, "session_id", "") or "").strip()
        if not umo:
            try:
                umo = str(event.get_sender_id() or "").strip()
            except Exception:  # noqa: BLE001
                umo = ""
        if str(self._cfg("session_scope", "group") or "group").strip().lower() != "user":
            return umo
        try:
            is_group = bool(event.get_group_id())
            sender = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            return umo
        if not is_group or not sender:
            return umo
        return f"{umo}#u{sender}"

    def _sent_this_turn(self, event: AstrMessageEvent) -> int:
        return self._turn_sends.get(self._convo_key(event), 0)

    def _count_sent(self, event: AstrMessageEvent) -> None:
        key = self._convo_key(event)
        self._turn_sends[key] = self._turn_sends.get(key, 0) + 1

    def _reset_turn(self, event: AstrMessageEvent) -> None:
        self._turn_sends[self._convo_key(event)] = 0

    def _is_permission_denied(self, exc: Exception) -> bool:
        text = str(exc)
        return "无权限" in text or "40034105" in text

    def _should_go_proactive(self, event: AstrMessageEvent) -> bool:
        """官方 Bot 是否改用主动消息。

        被动回复有「同一会话 5 分钟内最多回 5 次」的限制（40034128）；
        主动消息需要开放平台权益，没有时接口报 40034105「主动消息失败, 无权限」。
        """
        if not self._is_official_qq(event):
            return False
        mode = str(self._cfg("official_send_mode", "passive-first") or "passive-first").strip().lower()
        if mode == "passive":
            return False
        if self._proactive_denied:
            # 本进程内已知没有主动消息权限，别再每条都撞一次
            return False
        if mode == "proactive":
            return True
        # passive-first：本轮第一条仍作被动回复，之后转主动
        return self._sent_this_turn(event) >= 1

    async def _deliver(self, event: AstrMessageEvent, result):
        """统一发送出口：需要时把 msg_id 去掉走主动，失败回退被动重试一次。"""
        proactive = self._should_go_proactive(event)
        saved_id = None
        if proactive:
            saved_id = getattr(event.message_obj, "message_id", None)
            try:
                event.message_obj.message_id = None
            except Exception as exc:  # noqa: BLE001
                logger.warning("切换主动消息失败，保持被动：%s", exc)
                proactive = False
        started = time.monotonic()
        try:
            out = await event.send(result)
            self._count_sent(event)
            self._trace_deliver(event, proactive, started, out)
            return out
        except Exception as exc:  # noqa: BLE001
            if not proactive or saved_id is None:
                raise
            if self._is_permission_denied(exc):
                self._proactive_denied = True
                logger.warning("没有主动消息权限（40034105），本次及之后都改用被动回复")
            else:
                logger.warning("主动发送失败，回退被动重试：%s", exc)
            event.message_obj.message_id = saved_id
            out = await event.send(result)
            self._count_sent(event)
            self._trace_deliver(event, False, started, out)
            return out

    def _trace_on(self) -> bool:
        """诊断日志开关：默认**关**（0.3.7 起）。

        打开后每轮多几行 `[dsh-trace]`：SSE 事件到达时刻、每条正文的发送时刻、passive/active。
        正文「慢一拍」这类跨进程时序问题只能靠它定位，平时不用开。
        """
        return str(self._cfg("trace_delivery", "off") or "off").strip().lower() in {"on", "1", "true", "yes"}

    def _trace_deliver(self, event: AstrMessageEvent, proactive: bool, started: float, out) -> None:
        """诊断：正文「慢一拍」到底卡在哪一段（ingress / AstrBot / QQ）。看 `[dsh-trace]` 前缀。"""
        if not self._trace_on():
            return
        mid = getattr(out, "message_id", None) or getattr(out, "id", None)
        if not mid and isinstance(out, dict):
            mid = out.get("message_id") or out.get("id")
        via = "active" if proactive else "passive"
        convo = self._convo_key(event)
        logger.info(
            "[dsh-trace] send via=%s %.0fms convo=%s mid=%s",
            via,
            (time.monotonic() - started) * 1000,
            convo[-24:],
            mid,
        )

    def _trace_sse(self, data: dict, started: float) -> None:
        """诊断：每个 SSE 事件的到达时刻（相对本轮请求）。"""
        if not self._trace_on():
            return
        text = str(data.get("text") or data.get("message") or "")
        logger.info(
            "[dsh-trace] sse %s +%.0fms %s",
            data.get("_event"),
            (time.monotonic() - started) * 1000,
            text.replace("\n", " ⏎ ")[:60],
        )

    async def _pull_outbound(self, host_path: str, file_name: str, event: AstrMessageEvent) -> Path | None:
        """本地看不见这个文件时，向 ingress 要一张一次性凭证，自己把它拉过来。

        Docker 里 AstrBot 只挂了 `./data` 时，DSH 产出的文件它根本看不见 —— 这条兜底让
        「出站文件」也不再要求挂载 DSH 的盘。凭证由 ingress **按 umo 的工作区**签发，
        所以工作区外 / 敏感路径的校验不会因为走网络而放松。
        """
        if str(self._cfg("outbound_pull", True)).strip().lower() in {"off", "false", "0", "no", "none"}:
            return None
        try:
            limit_mb = float(self._cfg("outbound_pull_max_mb", 200) or 200)
        except (TypeError, ValueError):
            limit_mb = 200.0
        limit = int(max(1.0, limit_mb) * 1024 * 1024)
        url, bearer, _ = self._ingress_target()
        if not bearer:
            return None
        try:
            async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
                r = await client.post(
                    f"{url}/file-token",
                    json={"path": host_path, "umo": self._convo_key(event)},
                    headers={"Authorization": f"Bearer {bearer}"},
                )
                if r.status_code != 200:
                    logger.info("[dsh] 取拉取凭证失败（HTTP %s）：%s", r.status_code, r.text[:160])
                    return None
                payload = r.json() or {}
                token = str(payload.get("token") or "")
                size = int(payload.get("size") or 0)
                if not token or size > limit:
                    if size > limit:
                        logger.info("[dsh] 文件 %s 超过拉取上限（%s > %s）", file_name, size, limit)
                    return None
                dest_dir = Path("/AstrBot/data/temp") if Path("/AstrBot/data").exists() else Path("data/temp")
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / f"dsh-pull-{int(time.time() * 1000)}-{file_name}"
                written = 0
                async with client.stream(
                    "GET", f"{url}/file/{token}", headers={"Authorization": f"Bearer {bearer}"}
                ) as resp:
                    resp.raise_for_status()
                    with dest.open("wb") as fh:
                        async for chunk in resp.aiter_bytes():
                            written += len(chunk)
                            if written > limit:
                                raise RuntimeError("超过拉取上限")
                            fh.write(chunk)
            logger.info("[dsh] 出站文件从 ingress 拉取成功：%s（%d B）", file_name, written)
            return dest
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dsh] 拉取出站文件失败 %s：%s", file_name, exc)
            return None

    async def _send_outbound_file(self, event: AstrMessageEvent, src: Path, file_name: str) -> AsyncIterator[str]:
        """把本地文件发出去：图片走 Image 段、视频走 Video、其余走 File。"""
        try:
            size = src.stat().st_size
        except OSError:
            size = 0
        # 图片走 Image 段（AstrBot 自己 base64 上传），QQ 里直接显示，
        # 也不再需要协议端能读到的共享目录。
        if self._is_inline_image(file_name, size):
            try:
                await self._deliver(event, event.chain_result([Image.fromFileSystem(str(src))]))
            except Exception as exc:
                logger.warning("send image failed: %s", exc)
                yield f"发送图片失败：{file_name}（{exc}）"
            return
        send_path = str(src)
        if self._send_mode(event) == "shared":
            try:
                staged, err = self._stage_for_protocol(src, file_name)
            except OSError as exc:
                logger.warning("copy to protocol outbox failed: %s", exc)
                yield f"拷到协议端目录失败：{file_name}（{exc}）"
                return
            if err or not staged:
                yield f"{err or '无法把文件交给协议端'}：{file_name}"
                return
            send_path = staged
        if self._is_video_delivery(file_name):
            try:
                await self._deliver(event, event.chain_result([Video(file=send_path)]))
                return
            except Exception as exc:
                logger.warning("send video failed, fallback to file: %s", exc)
        try:
            await self._deliver(event, event.chain_result([File(name=file_name, file=send_path)]))
        except Exception as exc:
            logger.warning("send file failed: %s", exc)
            yield f"发送文件失败：{file_name}（{exc}）"

    async def _emit_sse(self, event: AstrMessageEvent, data: dict) -> AsyncIterator[str]:
        name = str(data.get("_event") or "message")
        if name == "file":
            host_path = str(data.get("path") or "")
            file_name = Path(str(data.get("name") or Path(host_path).name or "file")).name
            src = self._resolve_local_file(host_path)
            pulled: Path | None = None
            if src is None:
                # 容器里看不见 DSH 的文件（典型：只挂了 ./data）→ 从 ingress 拉一份过来
                pulled = await self._pull_outbound(host_path, file_name, event)
                src = pulled
            if src is None:
                yield f"要发的文件取不到：{file_name}（AstrBot 这边看不到该路径，从 ingress 拉取也没成功）"
                return
            try:
                async for item in self._send_outbound_file(event, src, file_name):
                    yield item
            finally:
                if pulled is not None:
                    try:
                        pulled.unlink(missing_ok=True)
                    except OSError:
                        pass
            return
        text_out = str(data.get("text") or data.get("message") or "")
        if name in {"text", "approval", "status", "error", "question"} and text_out:
            yield text_out

    def _mentions_someone_else(self, event: AstrMessageEvent) -> bool:
        """这条消息有没有 @ 到「别人」（不是机器人自己）。

        为什么需要它：`_quoted_is_dsh_content` 只能按**正文内容**判断「引的是不是 DSH 的回复」，
        所以「别人把机器人的正文复制成自己的消息、你再引用那条复制品」会被误判成续聊
        （2026-09-20 实测踩到）。而 `@某人 + 引用` 显然是在跟那个人说话，不是跟 DSH 说话 ——
        用这个信号把它挡掉。
        """
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:  # noqa: BLE001
            self_id = ""
        chain = list(getattr(getattr(event, "message_obj", None), "message", None) or [])
        for seg in chain:
            if not isinstance(seg, At):
                continue
            qq = str(getattr(seg, "qq", "") or "")
            if qq and qq != self_id and qq != "qq_official":
                return True
        # 官方通道：@ 的信息在原始 payload 的 `mentions` 里（AstrBot 有时把它留成字符串）
        payload = self._official_raw_payload(event)
        mentions = payload.get("mentions") if isinstance(payload, dict) else None
        if isinstance(mentions, list):
            for item in mentions:
                if not isinstance(item, dict):
                    continue
                if item.get("is_you") is True or item.get("bot") is True:
                    continue
                if str(item.get("id") or ""):
                    return True
            return False
        if isinstance(mentions, str) and mentions.strip():
            if re.search(r"['\"]?(?:is_you|bot)['\"]?\s*:\s*True", mentions):
                return False
            return bool(re.search(r"['\"]?id['\"]?\s*:\s*['\"][^'\"]+['\"]", mentions))
        return False

    def _is_explicit_bridge_command(self, text: str) -> bool:
        """是不是「明确在调桥」——只有这种才值得回一句「你不在白名单」。

        两类不算：

        - **回答类**（纯数字 / `批准` / `取消` / `yes`…）：它们是被 `_should_capture` 特意接住的
          （免得被人格当闲聊答掉），随便哪个群友打个 `1` 都回一句提示既吵、又等于告诉全群
          「这里有台 DSH」。
        - **引用 DSH 的回复**：名单外的人引用那条提示来问「这到底什么情况」时，再回一遍同样的
          提示就成了复读机（2026-09-20 实测在群里循环了一次）；他们本来也接不上会话。
          静默放行 → 消息落回人格，跟没装桥一样。

        只有真正的 `/dsh …`（或 `dsh …`）才提示。
        """
        if self._looks_like_approval(text):
            return False
        lowered = (text or "").strip().lower()
        body = lowered[1:] if lowered.startswith("/") else lowered
        cmd = self._command().lower()
        return body == cmd or body.startswith(cmd + " ")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=50)
    async def on_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        capture, payload = self._should_capture(event, text)
        if not capture:
            return
        if not self._allowed(event):
            # 只有明确在敲命令（`/dsh …`）才明说一句：否则 wake_prefix 含 "/" 时人格会去回答
            # "/dsh 帮我改代码" 这句。回答类与「引用 DSH 回复」都静默放行 —— 名单外的人本来也接不上。
            if event.get_group_id() and self._is_explicit_bridge_command(text):
                yield self._reply_result(event, "你不在 DSH 白名单里。管理员可在插件配置里添加 QQ 号 / 群号。")
                event.stop_event()
            # 私聊名单外 / 群里的普通消息：不回复也不 stop，让消息落回 AstrBot 人格正常接管
            return
        if not payload:
            yield self._reply_result(event, f"用法：/{self._command()} <任务>，或 /{self._command()} status")
            event.stop_event()
            return

        quiet = payload.lower() in {"stop", "status", "help", "ls", "list", "sessions", "ws", "new", "end", "model", "compact", "last", "perm"} \
            or payload.lower().startswith(("steer ", "use ", "ws ", "session ", "rename ", "send ", "model ", "last ", "perm ")) \
            or self._looks_like_approval(payload)
        self._reset_turn(event)
        trace_turn = time.monotonic()
        if not quiet:
            await self._deliver(event, self._reply_result(event, "已交给 DeepSeek Harness…"))
        async for chunk in self._forward(event, payload):
            if not isinstance(chunk, str) or not chunk.strip():
                continue
            result = await self._deliver(event, self._reply_result(event, chunk))
            mid = getattr(result, "message_id", None) or getattr(result, "id", None)
            if not mid and isinstance(result, dict):
                mid = result.get("message_id") or result.get("id")
            self._remember_outbound(mid, chunk)
            if self._trace_on():
                logger.info(
                    "[dsh-trace] body %d chars +%.0fms",
                    len(chunk),
                    (time.monotonic() - trace_turn) * 1000,
                )
        if self._trace_on():
            logger.info("[dsh-trace] turn done +%.0fms", (time.monotonic() - trace_turn) * 1000)
        event.stop_event()
