"""Discord 종합봇 (웹 OAuth 인증 + 티켓).

인증: 패널 [인증하기] → Discord 승인 → 웹사이트에서
이메일인증여부/중복IP 검사 → REST로 역할 지급.
티켓: 패널 [결제·주문 문의] / [일반·파트너 문의] → 1인 1개 전용 채널 → [닫기] → [삭제]/[재오픈].
로그: /입장로그 · /퇴장로그 → 각각 채널/켜기/이미지 독립 설정 (+ Pillow 전용 배너)
봇+웹이 한 프로세스로 뜸 (무료 호스팅 1서비스용).

실행:
  pip install -r requirements.txt
  (.env 작성)
  python bot.py  (봇 + 웹 :WEB_PORT 동시 실행)
"""

import asyncio
import json
import logging
import os
import re
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote as url_quote

import discord
from aiohttp import web
from discord import app_commands
from dotenv import load_dotenv

import joinleave
import recover
import ticket
from colors import COLOR_NAMES, HEX_HINT, parse_color
import webapi
import webverify

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")


# ---------- 설정 (.env) ----------
def _get_int(name: str) -> int | None:
    v = os.getenv(name, "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


@dataclass
class Config:
    token: str = os.getenv("DISCORD_TOKEN", "").strip()
    verified_role: str = os.getenv("VERIFIED_ROLE_NAME", "인증완료").strip()
    unverified_role: str = os.getenv("UNVERIFIED_ROLE_NAME", "미인증").strip()
    auth_channel_id: int | None = _get_int("AUTH_CHANNEL_ID")
    log_channel_id: int | None = _get_int("LOG_CHANNEL_ID")
    min_account_age_days: int = int(os.getenv("MIN_ACCOUNT_AGE_DAYS", "7") or 7)
    # 디스호스트는 SERVER_PORT 를 준다. 로컬/Render 는 PORT, 그 외 WEB_PORT.
    web_port: int = int(
        os.getenv("PORT", "")
        or os.getenv("WEB_PORT", "")
        or os.getenv("SERVER_PORT", "")
        or "8000"
    )

    def validate(self) -> list[str]:
        errors = []
        if not self.token:
            errors.append("DISCORD_TOKEN이 .env에 없습니다.")
        return errors


config = Config()

# ---------- 디스코드 클라이언트 ----------
intents = discord.Intents.default()
intents.members = True  # 특권 인텐트: 포털에서 꼭 켜기
intents.guilds = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# 티켓 — 재시작 후에도 버튼이 계속 동작하도록 persistent view 등록
ticket.bind(bot=bot, guild_fn=bot.get_guild)
bot.add_view(ticket.PanelView())
bot.add_view(ticket.LegacyPanelView())  # 버튼 1개짜리 구(舊) 패널이 계속 눌리도록
bot.add_view(ticket.TicketView())
bot.add_view(ticket.ClosedTicketView())
bot.add_view(ticket.TicketAdminView())  # 패널 [⚙️ 티켓 관리] → ephemeral 조작판

# 입장/퇴장 로그 관리 패널 (custom_id 에 kind 가 들어가 분리됨)
bot.add_view(joinleave.LogPanelView("join"))
bot.add_view(joinleave.LogPanelView("leave"))


def _guild_name(gid: int) -> str | None:
    g = bot.get_guild(gid)
    return g.name if g else None


def _bot_status() -> dict:
    try:
        if bot.is_closed():
            return {"bot": "down", "guilds": 0}
        return {"bot": "ready" if bot.is_ready() else "connecting", "guilds": len(bot.guilds)}
    except Exception:
        return {"bot": "unknown", "guilds": 0}


webverify.guild_name_fn = _guild_name
webverify.bot_status_fn = _bot_status

BASE_DIR = Path(__file__).parent
PANEL_FILE = BASE_DIR / "panels.json"


# ---------- 저장소 (panels.json) ----------
def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_panel_setting(message_id: int | None) -> dict | None:
    if message_id is None:
        return None
    return _load_json(PANEL_FILE).get(str(message_id))


# 서버 단위 역할 설정은 panels.json 대신 roles.json 에 둔다.
# panels.json 은 패널을 지우면 그 항목이 날아가고, 둘 다 .gitignore 대상이라
# 재업로드 때 통째로 덮어씌워지기 쉬우므로 한 파일에 몰아두지 않았다.
ROLES_FILE = BASE_DIR / "roles.json"


def get_guild_roles(guild_id: int) -> dict:
    raw = _load_json(ROLES_FILE).get(str(guild_id))
    return raw if isinstance(raw, dict) else {}


def _put_guild_setting(guild_id: int, key: str, value) -> None:
    """서버 설정 하나를 넣는다. value 가 None 이면 지우고, 항목이 비면 제거."""
    data = _load_json(ROLES_FILE)
    g = data.get(str(guild_id))
    if not isinstance(g, dict):
        g = {}
    if value is None:
        g.pop(key, None)
    else:
        g[key] = value
    if g:
        data[str(guild_id)] = g
    else:
        data.pop(str(guild_id), None)
    _save_json(ROLES_FILE, data)


def set_guild_role(guild_id: int, kind: str, role_id: int | None) -> None:
    """서버에 역할 하나를 지정/해제한다. (kind = verified / unverified)"""
    _put_guild_setting(guild_id, kind, role_id)


def get_panel_color(guild_id: int) -> int:
    """패널 왼쪽 바 색상. 아직 안 정했으면 기본 초록."""
    raw = get_guild_roles(guild_id).get("panel_color")
    if raw is None:
        return DEFAULT_PANEL_COLOR
    try:
        return int(raw) & 0xFFFFFF
    except (TypeError, ValueError):
        return DEFAULT_PANEL_COLOR


def set_panel_color(guild_id: int, value: int | None) -> None:
    """패널 왼쪽 바 색상 지정/초기화."""
    _put_guild_setting(
        guild_id, "panel_color", None if value is None else int(value) & 0xFFFFFF
    )


def get_verify_log_channel(guild_id: int) -> int | None:
    """인증 로그를 받을 채널. 봇이 이 채널로 **일반 메시지**를 보내므로
    디스코드 웹훅 429(IP 제한)와 무관하게 바로 도착한다."""
    raw = get_guild_roles(guild_id).get("log_channel")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def set_verify_log_channel(guild_id: int, channel_id: int | None) -> None:
    _put_guild_setting(guild_id, "log_channel", channel_id)


def get_err_channel(guild_id: int) -> int | None:
    """봇 오류 자동 보고를 받을 채널 (/오류보고)."""
    raw = get_guild_roles(guild_id).get("err_channel")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def set_err_channel(guild_id: int, channel_id: int | None) -> None:
    _put_guild_setting(guild_id, "err_channel", channel_id)


def _role_from(guild: discord.Guild, role_id) -> discord.Role | None:
    """ID 가 깨졌거나 삭제된 역할이면 None — 뒤 후보를 계속 살펴보게 한다."""
    try:
        return guild.get_role(int(role_id))
    except (TypeError, ValueError):
        return None


# ---------- 롤/로그 헬퍼 ----------
def resolve_role(guild: discord.Guild, name_or_id: str) -> discord.Role | None:
    s = name_or_id.strip()
    if not s:
        return None
    if s.isdigit():
        r = guild.get_role(int(s))
        if r:
            return r
    for r in guild.roles:
        if r.name == s or r.name.lower() == s.lower():
            return r
    return None


def resolve_verified_role(guild: discord.Guild, message_id: int | None = None) -> discord.Role | None:
    # 1) /인증관리 에서 직접 지정해 둔 역할
    r = _role_from(guild, get_guild_roles(guild.id).get("verified"))
    if r:
        return r
    # 2) 패널 게시 기록 — 패널마다 하나씩 보면서 실제로 존재하는 역할을 찾는다.
    #    (옛 구조는 첫 패널만 보고, 그 패널의 역할이 삭제돼 있으면 뒤를 못 봤다)
    panel = get_panel_setting(message_id)
    if panel:
        r = _role_from(guild, panel.get("verified"))
        if r:
            return r
    for v in _load_json(PANEL_FILE).values():
        if isinstance(v, dict) and v.get("guild") == guild.id:
            r = _role_from(guild, v.get("verified"))
            if r:
                return r
    # 3) .env
    return resolve_role(guild, config.verified_role)


def resolve_unverified_role(guild: discord.Guild) -> discord.Role | None:
    r = _role_from(guild, get_guild_roles(guild.id).get("unverified"))
    if r:
        return r
    return resolve_role(guild, config.unverified_role)


async def send_log(guild: discord.Guild, text: str):
    if not config.log_channel_id:
        return
    ch = guild.get_channel(config.log_channel_id)
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(text)
        except discord.Forbidden:
            log.warning("로그 채널 전송 권한 없음")


# ---------- 인증 패널 (웹 링크 버튼) ----------
DEFAULT_TITLE = "🔐 멤버 인증"
DEFAULT_LINE1 = "서버에 오신 걸 환영합니다!"
DEFAULT_LINE2 = "아래 **인증하기** 버튼을 눌러주세요.\n인증하면 나머지 채널이 보입니다."
DEFAULT_PANEL_COLOR = 0x57F287  # 초록 — 기존 패널과 같은 색


def build_panel(
    title: str | None = None,
    line1: str | None = None,
    line2: str | None = None,
    color: int | None = None,
) -> discord.Embed:
    """유저용 인증 패널 임베드. (지급 역할 문구는 넣지 않는다)"""
    title = (title or DEFAULT_TITLE)[:200]
    line1 = (line1 or DEFAULT_LINE1)[:1000]
    line2 = (line2 or DEFAULT_LINE2)[:1500]
    value = (color if color is not None else DEFAULT_PANEL_COLOR) & 0xFFFFFF
    embed = discord.Embed(
        title=title,
        description=f"{line1}\n\n{line2}",
        color=discord.Color(value),
    )
    return embed


def build_panel_url(guild: discord.Guild, role: discord.Role) -> str:
    """패널 버튼에 붙는 웹 인증 링크. 지급 역할이 바뀌면 이것도 다시 짜야 한다."""
    base = webverify.env("WEB_PUBLIC_URL").rstrip("/")
    gparam = f"&g={url_quote(guild.name[:50], safe='')}"
    iparam = f"&i={guild.icon.key}" if guild.icon else ""
    return f"{base}/?guild={guild.id}&role={role.id}{gparam}{iparam}"


async def _locate_panel(
    guild: discord.Guild, mid: int, hint_channel
) -> discord.Message | None:
    ch = None
    if hint_channel:
        try:
            ch = guild.get_channel(int(hint_channel))
        except (TypeError, ValueError):
            ch = None
    if isinstance(ch, (discord.TextChannel, discord.Thread)):
        try:
            return await ch.fetch_message(mid)
        except discord.HTTPException:
            pass
    # 구버전 패널은 채널 기록이 없다 → 텍스트 채널을 하나씩 뒤져서 찾는다.
    for c in guild.text_channels[:40]:
        try:
            return await c.fetch_message(mid)
        except discord.HTTPException:
            continue
    return None


# ---------- 패널 자동 발견 ----------
# panels.json 은 Git 대상이 아니라 서버 로컬 파일이라 호스팅을 옮기거나
# 컨테이너가 초기화되면 통째로 비어진다. 그럴 때마다 /인증패널 /티켓패널 을
# 다시 치지 않도록, 채널에 살아있는 패널을 스스로 되찾는다.
_OAUTH_ROLE_RE = re.compile(r"[?&]role=(\d{15,})")
_SCAN_CHANNELS = 40
_SCAN_HISTORY = 50


def _panel_gone(guild: discord.Guild, hint_channel) -> bool:
    """기록된 채널이 그대로 있고 봇도 읽을 수 있는데 메시지를 못 찾았으면 삭제된 것.

    권한이 없어서 못 읽는 경우와 삭제된 경우를 가르기 위함 — 전자는 건드리지 않는다.
    """
    try:
        ch = guild.get_channel(int(hint_channel)) if hint_channel else None
    except (TypeError, ValueError):
        return False
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return False
    me = guild.me
    if me is None:
        return False
    perms = ch.permissions_for(me)
    return bool(perms.view_channel and perms.read_message_history)


def _iter_buttons(components):
    """메시지 컴포넌트 트리를 재귀로 훑어 버튼만 거둔다.

    버튼은 ActionRow(·Container) 안에 들어 있고, discord.py 에서 안쪽 목록의
    속성 이름은 `.children` 이다. `.components` 로 읽으면 속성이 없어 항상
    빈 목록이 나오고 → 버튼을 한 개도 못 본다. 이 자리가 틀리면 자동 발견이
    조용히 0건으로 끝난다.
    """
    for comp in components or ():
        kids = getattr(comp, "children", None)
        if kids:
            yield from _iter_buttons(kids)
        elif hasattr(comp, "url") and hasattr(comp, "custom_id"):
            yield comp  # 버튼에만 url/custom_id 가 둘 다 있다


def _verify_panel_record(msg: discord.Message) -> dict | None:
    """이 메시지가 인증 패널이면 기록용 dict, 아니면 None.

    판정 근거: 임베드 1개 + 링크 버튼에 웹 인증 주소(guest=role 파라미터)가 붙은 것.
    그 밖의 봇 메시지(환영/공지/로그)는 전부 걸러진다.
    """
    if len(msg.embeds) != 1 or msg.guild is None:
        return None
    url = None
    for item in _iter_buttons(msg.components):
        u = getattr(item, "url", None)
        if u and "guild=" in u and "role=" in u:
            url = u
            break
    if not url:
        return None

    m = _OAUTH_ROLE_RE.search(url)
    role_id = int(m.group(1)) if m else None
    if role_id is None:
        role_id = get_guild_roles(msg.guild.id).get("verified")

    e = msg.embeds[0]
    desc = e.description or ""
    line1, line2 = desc.split("\n\n", 1) if "\n\n" in desc else (desc, "")
    # 기본값과 똑같은 줄은 굳이 적지 않는다 → 나중에 기본 문구를 고치면 패널도 따라간다.
    return {
        "guild": msg.guild.id,
        "channel": msg.channel.id,
        "verified": role_id,
        "title": None if (e.title or "") == DEFAULT_TITLE else e.title,
        "line1": None if not line1 or line1 == DEFAULT_LINE1 else line1,
        "line2": None if not line2 or line2 == DEFAULT_LINE2 else line2,
    }


async def discover_verify_panels(guild: discord.Guild) -> int:
    """채널에 살아있는 인증 패널을 찾아 panels.json 에 되찾는다. 새로 등록된 개수."""
    me = guild.me
    author_id = me.id if me else (bot.user.id if bot.user else 0)
    if not author_id:
        return 0
    panels = _load_json(PANEL_FILE)
    added = 0
    for ch in guild.text_channels[:_SCAN_CHANNELS]:
        if me is not None and not ch.permissions_for(me).read_message_history:
            continue
        try:
            messages = [m async for m in ch.history(limit=_SCAN_HISTORY)]
        except discord.HTTPException:
            continue
        for msg in messages:
            if msg.author.id != author_id or str(msg.id) in panels:
                continue
            rec = _verify_panel_record(msg)
            if rec is None:
                continue
            panels[str(msg.id)] = rec
            added += 1
    if added:
        _save_json(PANEL_FILE, panels)
        log.info("인증 패널 자동 발견 %s개 guild=%s", added, guild.id)
    return added


async def refresh_verify_panels(
    guild: discord.Guild, role: discord.Role | None = None
) -> tuple[int, int, int]:
    """이 서버에 게시된 인증 패널 전부를 다시 쓴다.

    패널 문구(역할 줄은 이제 없음)와 왼쪽 바 색상을 새로 그리고,
    role 을 주면 버튼 URL 안의 role 파라미터도 같이 바꾼다.
    role 을 못 찾았을 땐 URL 은 건드리지 않고 임베드만 고친다.
    반환: (수정됨, 실패, 찾지못함)
    """
    panels = _load_json(PANEL_FILE)
    color = get_panel_color(guild.id)
    ok = fail = missing = 0
    changed = False
    for mid_s, e in list(panels.items()):
        if not isinstance(e, dict) or e.get("guild") != guild.id:
            continue
        try:
            mid = int(mid_s)
        except ValueError:
            continue
        msg = await _locate_panel(guild, mid, e.get("channel"))
        if msg is None:
            missing += 1
            # 읽기 권한이 있는데도 못 찾았으면 메시지가 지워진 것 → 목록에서 정리.
            # (권한 문제로 못 읽는 경우는 남겨둬야 다음에 또 찾을 수 있다)
            if _panel_gone(guild, e.get("channel")):
                panels.pop(mid_s, None)
                changed = True
            continue
        embed = build_panel(e.get("title"), e.get("line1"), e.get("line2"), color)
        try:
            if role is None:
                await msg.edit(embed=embed)
            else:
                await msg.edit(
                    embed=embed, view=VerifyPanelView(build_panel_url(guild, role))
                )
        except discord.HTTPException:
            fail += 1
            continue
        if e.get("channel") != msg.channel.id:
            e["channel"] = msg.channel.id  # 채널 기록 백필
            changed = True
        ok += 1
    if changed:
        _save_json(PANEL_FILE, panels)
    return ok, fail, missing


# ---------- 로그 대기열 봇이 대신 전송 ----------
# Render IP 는 디스코드 웹훅 POST 제한(429)을 받는 경우가 있어
# 웹에서 직접 못 보낸 인증 로그는 대기열에 남고, 봇은 여기서 꺼내 직접 보낸다.
def _queue_interval() -> float:
    try:
        v = float(os.getenv("LOG_QUEUE_INTERVAL", "5") or 5)
    except ValueError:
        v = 5.0
    return min(300.0, max(1.0, v))


_LOG_QUEUE_INTERVAL = _queue_interval()
_LOG_PUMP_TASK: asyncio.Task | None = None
# 마지막 대기열 처리 결과 (/인증로그 → 🔍 로그 상태 에서 보여줌)
_LOG_PUMP_LAST: dict = {"ts": 0, "taken": 0, "sent": 0, "failed": 0, "detail": ""}


async def _send_to_log_channel(guild_id: int, embed_dict: dict) -> str:
    """로그 채널로 **일반 메시지**를 보낸다. 'sent' / 'skip' / 'fail' 반환.

    웹훅이 아니라 평문 메시지라 디스코드 웹훅 429(IP 제한)와 상관없이 바로 간다.
    """
    ch_id = get_verify_log_channel(guild_id)
    if not ch_id:
        return "skip"  # 채널 미지정 → 실패 아님 (웹훅만 쓰거나 둘 다 없음)
    ch = bot.get_channel(ch_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(ch_id)
        except (discord.NotFound, discord.Forbidden):
            log.warning("인증 로그 채널 접근 불가 guild=%s ch=%s", guild_id, ch_id)
            set_verify_log_channel(guild_id, None)  # 사라진 채널 설정은 자동 정리
            return "skip"
        except discord.HTTPException as e:
            log.warning("인증 로그 채널 조회 실패: HTTP %s", e.status)
            return "fail"
    if not isinstance(ch, discord.abc.Messageable):
        return "skip"
    try:
        await ch.send(embed=discord.Embed.from_dict(embed_dict))
        return "sent"
    except discord.HTTPException as e:
        log.warning("인증 로그 채널 전송 실패: HTTP %s", e.status)
        return "fail"


# 마지막 전송 실패 상세 (상태 화면용)
_LAST_SEND_DETAIL = ""


async def _process_queue_item(item: dict) -> tuple[int, int]:
    """대기열 항목 하나를 전송한다. (성공 수, 실패 수) 반환. 실패분은 다시 투입."""
    global _LAST_SEND_DETAIL
    embed = item.get("embed")
    if not isinstance(embed, dict):
        return 0, 0
    tries = int(item.get("tries", 0)) + 1
    sent = failed = 0
    pending: list[str] = []
    needs_retry = False

    # 1) 로그 채널 — 봇이 **일반 메시지**로 직접 (웹훅 429 와 무관)
    try:
        gid = int(str(item.get("guild") or "").strip() or 0)
    except ValueError:
        gid = 0
    if gid:
        res = await _send_to_log_channel(gid, embed)
        if res == "sent":
            sent += 1
        elif res == "fail":
            failed += 1
            needs_retry = True
            _LAST_SEND_DETAIL = "로그 채널 전송 실패"

    # 2) 웹훅 — 봇 IP 로 POST
    for url in item.get("urls") or []:
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        ok, detail = await webverify.post_webhook(url, {"embeds": [embed]})
        if ok:
            sent += 1
        else:
            failed += 1
            needs_retry = True
            _LAST_SEND_DETAIL = detail
            log.warning("로그 웹훅 전송 실패(시도 %s): %s", tries, detail)
            pending.append(url)

    if needs_retry:
        try:
            await webapi.queue_put({
                "embed": embed,
                "guild": item.get("guild", ""),
                "urls": pending,
                "tries": tries,
            })
        except Exception as e:
            log.warning("로그 대기열 재투입 실패: %s", e)
    return sent, failed


# 웹 API 거부(401 등) 경고 마지막 시각 — 같은 이유로 5초마다 도배하지 않기 위함
_API_FAIL_LOG_AT = 0.0


async def _pump_log_queue() -> None:
    """대기열 로그를 봇이 단독 전송한다. 절대 죽지 않게 예외는 모두 삼킨다.

    로그 채널은 일반 메시지로, 웹훅은 봇 IP 로 POST 한다.
    항목이 있었으면 1초 뒤에 또 받아오고(연속 인증 대응), 없으면 주기만큼 쉰다.
    """
    global _LOG_PUMP_LAST, _API_FAIL_LOG_AT
    await asyncio.sleep(5)
    while True:
        delay = _LOG_QUEUE_INTERVAL
        try:
            items = await webapi.queue_take()
            if items:
                delay = 1.0  # 뭔가 들어왔으면 바로 또 확인 (연속 인증 로그 지연 없앰)
            sent = failed = 0
            for item in items:
                s, f = await _process_queue_item(item)
                sent += s
                failed += f
            _LOG_PUMP_LAST = {
                "ts": int(time.time()), "taken": len(items),
                "sent": sent, "failed": failed, "detail": _LAST_SEND_DETAIL,
            }
        except webapi.ApiError as e:
            # 서버가 응답을 했는데 거부한 경우(401 등)다.
            # 스택트레이스를 5초마다 찍지 않도록 한 줄 경고로 바꾸고
            # 같은 이유는 재시도 주기도 60초로 벌려준다.
            detail = " ".join(str(e).split())
            delay = 60.0
            _LOG_PUMP_LAST = {
                "ts": int(time.time()), "taken": 0, "sent": 0, "failed": 0,
                "detail": detail,
            }
            now = time.time()
            if now - _API_FAIL_LOG_AT >= 60:
                _API_FAIL_LOG_AT = now
                if "unauthorized" in detail.lower():
                    log.warning(
                        "웹(Render) API 인증 거부(401) — 봇 WEB_SECRET 과 "
                        "Render Env WEB_SECRET 이 다릅니다. "
                        "/인증로그 → [🔍 로그 상태] 에서 확인하세요 (1분에 1회만 표시)"
                    )
                else:
                    log.warning("웹(Render) API 오류 (1분에 1회만 표시): %s", detail)
        except Exception:
            log.exception("로그 대기열 전송 중 오류")
        await asyncio.sleep(delay)


def _start_log_pump() -> None:
    global _LOG_PUMP_TASK
    if _LOG_PUMP_TASK is not None and not _LOG_PUMP_TASK.done():
        return
    _LOG_PUMP_TASK = asyncio.create_task(_pump_log_queue())


# ---------- 오류 자동 보고 (/오류보고) ----------
# 봇이 오류로 조용히 죽어도 운영자가 먼저 알 수 있도록 ERROR 이상 로그를 모아
# /오류보고 로 지정한 채널로 보낸다. (호스팅 콘솔은 글자를 못 읽으니 여기가 유일한 창구)
#
# 설계 포인트
#  · logging.Handler 는 await 를 못 한다 → 핸들러는 **큐에만** 넣는다.
#    전송은 별도 태스크(_pump_err_reports) 가 주기적으로 비워한다.
#  · 재접속 때마다 태스크가 새로 생기지 않게 플래그로 막는다.
#  · 같은 오류 60초 중복 억제 + 최소 3초 전송 간격 (디스코드 rate limit 보호)
#  · 이 태스크 자체가 죽으면 오류 보고가 영영 안 되므로 어떤 경우에도 종료하지 않는다.

_ERR_QUEUE: list[dict] = []          # 대기 중인 오류
_ERR_SEEN: dict[str, float] = {}     # 중복 억제용 key -> 시각
_ERR_STATE = {"sent": 0, "suppressed": 0, "last_sent": 0.0, "last_error": ""}
_ERR_TASK: asyncio.Task | None = None
_ERR_DEDUP_WINDOW = 60.0
_ERR_MIN_GAP = 3.0
_ERR_QUEUE_MAX = 50
_ERR_LOGGER_NAME = "bot.errreport"
# 슬래시 오류는 서버 컨텍스트를 안다 → 그 서버 채널로만 보낸다(유저 정보 새는 것 방지).
# 로깅 핸들러는 컨텍스트를 모른다 → on_app_error 가 이 값을 잠깐 넣어두고 핸들러가 가져간다.
_ERR_CTX_GUILD: int | None = None


def _err_targets() -> dict[int, int]:
    """오류 보고 채널로 지정된 곳. {채널 ID: 서버 ID}."""
    out: dict[int, int] = {}
    try:
        data = _load_json(ROLES_FILE)
    except Exception:
        return out
    for gid, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        try:
            cid = int(cfg.get("err_channel"))
            g = int(gid)
        except (TypeError, ValueError):
            continue
        if cid and g:
            out[cid] = g
    return out


class _ErrorReportHandler(logging.Handler):
    """ERROR 이상 로그를 수집만 한다. 전송은 _pump_err_reports 가 한다."""

    def emit(self, record: logging.LogRecord) -> None:
        global _ERR_CTX_GUILD
        # 로깅 핸들러는 절대 예외를 밖으로 던지면 안 된다 (봇 전체가 죽을 수 있음).
        try:
            if record.levelno < logging.ERROR:
                return
            if record.name == _ERR_LOGGER_NAME:
                return  # 자기 자신이 만든 보고는 다시 보고하지 않는다
            if not _err_targets():
                return  # 아무도 안 받으면 애초에 모으지 않는다
            msg = record.getMessage()
            key = f"{record.name}|{record.levelname}|{msg[:200]}"
            now = time.time()
            for k in [k for k, ts in _ERR_SEEN.items() if now - ts > 300]:
                _ERR_SEEN.pop(k, None)
            if now - _ERR_SEEN.get(key, 0.0) < _ERR_DEDUP_WINDOW:
                _ERR_STATE["suppressed"] += 1
                return
            _ERR_SEEN[key] = now
            if record.exc_info:
                tb = "".join(traceback.format_exception(*record.exc_info))
            elif record.exc_text:
                tb = str(record.exc_text)
            else:
                tb = str(record.stack_info or "")
            _ERR_QUEUE.append(
                {
                    "name": record.name,
                    "level": record.levelname,
                    "msg": msg,
                    "tb": tb,
                    "file": record.pathname or "",
                    "line": record.lineno or 0,
                    "guild": _ERR_CTX_GUILD,
                    "ts": now,
                }
            )
            _ERR_CTX_GUILD = None  # 소비했다 → 다음 오류로 새는 것을 막는다
            if len(_ERR_QUEUE) > _ERR_QUEUE_MAX:
                del _ERR_QUEUE[:-_ERR_QUEUE_MAX]
        except Exception:
            pass


def _err_embed(item: dict) -> discord.Embed:
    lines = [ln for ln in (item.get("tb") or "").splitlines() if ln.strip()]
    # 오류가 터진 줄: traceback 안의 마지막 File 프레임
    where = ""
    for ln in reversed(lines):
        s = ln.strip()
        if s.startswith('File "'):
            where = s
            break
    if not where:
        where = f"`{os.path.basename(item.get('file') or '?')}` {item.get('line') or ''}".strip()
    tail = lines[-1].strip() if lines else str(item.get("msg", ""))[:200]
    body = "\n".join(lines[-8:])
    if len(body) > 900:
        body = "…" + body[-900:]

    # footer·우하단 시각 표시는 사용자 요구로 전면 금지 (테스트가 소스 스캔으로 검증)
    e = discord.Embed(
        title="🚨 봇 오류",
        description=f"**{str(item.get('msg', ''))[:400]}**",
        color=discord.Color.red(),
    )
    e.add_field(name="터진 위치", value=where[:300] or "-", inline=False)
    e.add_field(name="마지막 줄", value=f"`{tail[:200]}`", inline=False)
    if body:
        e.add_field(name="스택", value=f"```py\n{body}\n```", inline=False)
    e.add_field(name="소스", value=f"`{item.get('name', '?')}`", inline=True)
    if item.get("guild"):
        e.add_field(name="서버", value=f"`{item['guild']}`", inline=True)
    return e


async def _deliver_err(item: dict) -> None:
    targets = _err_targets()
    gid = item.get("guild")
    if gid is not None:
        # 서버를 아는 오류는 그 서버 채널로만 (다른 서버 관리자에게 유저 정보가 새는 것을 막는다)
        ch_id = get_err_channel(int(gid))
        targets = {ch_id: int(gid)} if ch_id else {}
    if not targets:
        return
    embed = _err_embed(item)
    for ch_id, guild_id in targets.items():
        ch = bot.get_channel(ch_id)
        if ch is None:
            try:
                ch = await bot.fetch_channel(ch_id)
            except (discord.NotFound, discord.Forbidden):
                log.info("오류 보고 채널 사라짐 → 설정 정리 ch=%s", ch_id)
                set_err_channel(guild_id, None)  # 죽은 설정은 자동 정리
                continue
            except discord.HTTPException as e:
                _ERR_STATE["last_error"] = f"조회 실패 HTTP {e.status}"
                continue
        if not isinstance(ch, discord.abc.Messageable):
            continue
        try:
            await ch.send(embed=embed)
            _ERR_STATE["sent"] += 1
        except discord.HTTPException as e:
            _ERR_STATE["last_error"] = f"HTTP {e.status}"


async def _pump_err_reports() -> None:
    while not bot.is_closed():
        try:
            await asyncio.sleep(2.0)
            if not _ERR_QUEUE:
                continue
            if time.time() - _ERR_STATE["last_sent"] < _ERR_MIN_GAP:
                continue  # 속도 제한 → 다음 사이클로 미룬다
            item = _ERR_QUEUE.pop(0)
            _ERR_STATE["last_sent"] = time.time()
            try:
                await _deliver_err(item)
            except Exception as e:
                _ERR_STATE["last_error"] = str(e)[:200]
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # 이 태스크가 죽으면 오류 보고가 영영 안 되므로 절대 죽이지 않는다


def _start_err_reports() -> None:
    global _ERR_TASK
    if _ERR_TASK is not None and not _ERR_TASK.done():
        return
    _ERR_TASK = asyncio.create_task(_pump_err_reports())


# 루트 로거에 붙인다 → discord / asyncio / 자기 코드 어디서 나든 다 잡는다.
logging.getLogger().addHandler(_ErrorReportHandler())


# ---------- 이벤트 ----------
_PANEL_REFRESH_DONE = False


async def _refresh_all_panels() -> None:
    """서버에 게시된 인증/티켓 패널을 지금 규칙(footer·시각 제거, 색상, 버튼)으로 다시 그린다.

    레지스트리가 비어있어도 채널에서 스스로 되찾은 뒤 다시 그리기 때문에
    파일이 초기화돼도 /인증패널 /티켓패널 을 다시 칠 필요가 없다.
    """
    for g in list(bot.guilds):
        # 1) 목록 복구 — 빈 파일·누락된 항목을 채널에서 되찾는다
        for name, finder in (("인증", discover_verify_panels), ("티켓", ticket.discover_panels)):
            try:
                await finder(g)
            except Exception:
                log.exception("%s 패널 자동 발견 실패 guild=%s", name, getattr(g, "id", "?"))
        # 2) 지금 규칙으로 다시 그리기
        jobs = (
            ("인증", lambda gg=g: refresh_verify_panels(gg, resolve_verified_role(gg))),
            ("티켓", lambda gg=g: ticket.refresh_panels(gg)),
        )
        for name, job in jobs:
            try:
                ok, fail, missing = await job()
            except Exception:
                log.exception("%s 패널 갱신 실패 guild=%s", name, getattr(g, "id", "?"))
                continue
            if ok or fail or missing:
                log.info(
                    "%s 패널 갱신 guild=%s ok=%s fail=%s miss=%s",
                    name, g.id, ok, fail, missing,
                )


@bot.event
async def on_ready():
    global _PANEL_REFRESH_DONE
    errors = config.validate()
    if os.getenv("DISABLE_WEB", "").lower() not in ("1", "true", "yes"):
        errors += webverify.validate()  # 봇 전용 모드에서는 웹 키 불필요
    if errors:
        log.error("설정 오류: %s", errors)
    else:
        log.info("로그인: %s", bot.user)
    # 전역(global) 동기화만 한다.
    # 길드(서버) 단위 동기화를 함께 돌리면 같은 이름의 명령이 전역/길드 두 벌로
    # 남아서 슬래시 메뉴에 2개씩 표시된다. 전역 하나면 어떤 서버에서든 자동으로 뜬다.
    try:
        await tree.sync()
        log.info("슬래시 명령 동기화 완료 (전역, 최대 1시간 내 반영)")
    except Exception as e:
        log.exception("슬래시 명령 동기화 실패: %s", e)
    # 재접속 때마다 도는 걸 막기 위한 한 번만 플래그.
    # 패널 양식(역할 문구 제거·색상)이 배포 직후 바로 반영되게 한다.
    if not _PANEL_REFRESH_DONE:
        _PANEL_REFRESH_DONE = True
        await _refresh_all_panels()
    # 웹(Render)이 직접 못 보낸 인증 로그를 봇 IP 로 대신 전송하도록 띄운다.
    _start_log_pump()
    # 오류 자동 보고 펌프 (/오류보고)
    _start_err_reports()


@bot.event
async def on_guild_join(guild: discord.Guild):
    """봇 초대 시 복구키 발급 → 초대한 사람 DM."""
    me = bot.user
    assert me is not None
    inviter = await recover.find_inviter(guild, me.id)
    target = inviter or guild.owner
    key, _ = recover.ensure_key(guild.id, target.id if target else "?")
    text = (
        f"🔑 **{guild.name} 서버의 복구키가 발급됐습니다**\n"
        f"`{key}`\n\n"
        f"• 앞으로 이 서버에서 웹 인증하는 사람이 이 키 앞으로 자동 누적됩니다.\n"
        f"• 서버가 테러당하면 **다른 서버**에서 `/복구 키:{key}` → 멤버들을 이 서버로 재초대합니다.\n"
        f"• `/복구현황 키:{key}` 로 쌓인 인원을 확인할 수 있습니다.\n"
        f"⚠️ 키가 유출되면 anyone이 멤버를 빼갈 수 있으니 절대 공유하지 마세요!"
    )
    if target is None:
        await send_log(guild, f"🔑 복구키 발급됨 (받을 사람 없음): `{key}`")
        return
    try:
        await target.send(text)
        log.info("recovery key DM sent: guild=%s to=%s", guild.id, target.id)
    except (discord.Forbidden, discord.HTTPException):
        await send_log(guild, f"🔑 {target.mention}님 DM이 막혀있어요. 복구키: `{key}` (확인 후 삭제하세요!)")


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    verified = resolve_verified_role(guild)
    unverified = resolve_unverified_role(guild)

    try:
        if unverified:
            await member.add_roles(unverified, reason="입장 - 미인증 부여")
        if verified and verified in member.roles:
            await member.remove_roles(verified, reason="재입장 - 인증 초기화")
    except discord.Forbidden:
        log.warning("롤 부여 권한 없음 (봇 롤 순서 확인)")

    warn = ""
    if config.min_account_age_days > 0:
        age_days = (datetime.now(timezone.utc) - member.created_at).days
        if age_days < config.min_account_age_days:
            warn = f" ⚠️ 계정생성 {age_days}일 (기준 {config.min_account_age_days}일 미만)"

    # 입장 로그: /입장로그 에 채널이 잡혀 있으면 임베드(+배너), 아니면 기존 텍스트 로그
    if not await joinleave.send(member, "join", note=warn.strip() or None):
        await send_log(guild, f"👋 **입장** {member.mention} (`{member}`){warn}")

    if config.auth_channel_id:
        ch = guild.get_channel(config.auth_channel_id)
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(
                    f"{member.mention}님 환영합니다! 위 **인증하기** 버튼을 눌러주세요. 🎉",
                    delete_after=30,
                )
            except discord.HTTPException:
                pass


@bot.event
async def on_member_remove(member: discord.Member):
    """퇴장 로그. joinleave 미설정이면 기존 LOG_CHANNEL_ID 텍스트 로그로 대체."""
    if not await joinleave.send(member, "leave"):
        await send_log(member.guild, f"👋 **퇴장** {member.mention} (`{member}`)")


# ---------- 슬래시 명령 ----------
@tree.command(name="인증패널", description="인증 패널 게시 + 역할/문구 지정 (관리자)")
@app_commands.describe(
    채널="유저용 인증 패널을 올릴 텍스트 채널 (비우면 현재 채널)",
    역할="인증하면 지급할 역할 (비우면 .env 기본값)",
    제목="패널 제목 (비우면 기본값)",
    문장1="맨 위 문장: 환영 문구 (비우면 기본값)",
    문장2="아래 블록 전체 (버튼 안내+채널 안내, 비우면 기본값)",
    색상="패널 왼쪽 바 색상 (예: #57F287 또는 그린·블루, 비우면 현재 설정 유지)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_panel(
    interaction: discord.Interaction,
    채널: discord.abc.GuildChannel | None = None,
    역할: discord.Role | None = None,
    제목: str | None = None,
    문장1: str | None = None,
    문장2: str | None = None,
    색상: str | None = None,
):
    target = 채널 or interaction.channel
    if isinstance(target, discord.Thread):
        target = target.parent
    if not isinstance(target, discord.TextChannel):
        got = getattr(target, "mention", "선택한 항목")
        await interaction.response.send_message(
            f"❌ {got}에는 패널을 올릴 수 없어요.\n"
            f"**채널** 칸에는 일반 **텍스트 채널**을 골라주세요. (음성/포럼/공지/카테고리·역할 불가)\n"
            f"비워두면 명령을 입력한 현재 채널에 게시됩니다.",
            ephemeral=True,
        )
        return
    guild = interaction.guild
    assert guild is not None
    me = guild.me
    verified = 역할 or resolve_verified_role(guild)
    if verified is None:
        await interaction.response.send_message(
            "❌ 인증 롤을 찾을 수 없어요. `역할:`을 지정하거나 .env의 VERIFIED_ROLE_NAME을 확인해주세요.",
            ephemeral=True,
        )
        return
    if me and me.top_role <= verified:
        await interaction.response.send_message(
            f"❌ {verified.mention}이(가) 봇 롤보다 높아서 지급할 수 없어요. 서버 설정 → 역할에서 봇 롤을 위로 올리세요.",
            ephemeral=True,
        )
        return
    color = get_panel_color(guild.id)
    color_set = False
    if 색상 is not None:
        parsed = parse_color(색상)
        if parsed is None:
            await interaction.response.send_message(
                "❌ 색상을 못 읽어요. `#57F287` 처럼 HEX 6자리, "
                "또는 그린/블루/레드 같은 색 이름으로 적어주세요.",
                ephemeral=True,
            )
            return
        set_panel_color(guild.id, parsed)
        color = parsed
        color_set = True
    # Discord 는 최초 응답에 3초만 준다. 그 안에 답 못하면
    # 10062 Unknown interaction 으로 응답이 통째로 버려진다.
    await interaction.response.defer(ephemeral=True)
    try:
        await webapi.api_ping()
    except webapi.ApiError as e:
        await interaction.followup.send(
            f"❌ 웹 서버에 연결할 수 없습니다: {e}",
            ephemeral=True,
        )
        return
    # 랜딩 페이지를 거쳐 승인 → 스코프가 바뀌어도 기존 패널이 그대로 유효
    # 서버명/아이콘도 링크에 포함 (웹에서 API 조회 없이 표시)
    url = build_panel_url(guild, verified)
    embed = build_panel(제목, 문장1, 문장2, color)
    msg = await target.send(embed=embed, view=VerifyPanelView(url))
    panels = _load_json(PANEL_FILE)
    panels[str(msg.id)] = {
        "guild": guild.id,
        "channel": target.id,
        "verified": verified.id,
        "title": 제목,
        "line1": 문장1,
        "line2": 문장2,
    }
    # 패널에 지정한 역할을 서버 기본값으로도 남긴다.
    # → 패널 메시지를 지워도 /인증관리 와 입장 로그가 같은 역할을 쓴다.
    set_guild_role(guild.id, "verified", verified.id)
    _save_json(PANEL_FILE, panels)
    extra = f"패널 색상: `#{color:06X}`\n" if color_set else ""
    await interaction.followup.send(
        f"{target.mention}에 인증 패널을 올렸어요. ✅\n"
        f"지급 역할: {verified.mention}\n"
        f"{extra}"
        f"관리 메뉴는 **/인증관리** 로 열 수 있어요.",
        ephemeral=True,
    )


@tree.command(name="인증관리", description="인증 관리 조작판 (관리자, 본인 화면에만 뜹니다)")
@app_commands.checks.has_permissions(manage_guild=True)
async def verify_admin_panel(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    await interaction.response.send_message(
        embed=verify_admin_embed(guild), view=VerifyAdminView(), ephemeral=True
    )


# ---------- 인증 관리 (인증패널의 버튼 → 모달 / ephemeral) ----------
# 슬래시명령 복구키확인·복구현황·복구·인증초기화·인증로그·인증로그해제 를
# 여기 버튼으로 옮겨왔다. 권한은 관리자(서버관리 또는 역할관리).

_PENDING_RECOVER: dict[int, str] = {}  # 유저 id -> 복구키 (실행 확인용)


def _verify_admin_ok(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    p = user.guild_permissions
    return bool(p.manage_guild or p.manage_roles)


async def _deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "❌ 권한이 없어요 (서버 관리자 전용).", ephemeral=True
    )


async def _resolve_member(guild: discord.Guild, raw: str) -> discord.Member | None:
    """멘션/ID 어느 쪽이든 숫자만 뽑아 멤버를 찾는다."""
    m = re.search(r"\d{15,25}", raw or "")
    if not m:
        return None
    uid = int(m.group(0))
    member = guild.get_member(uid)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(uid)
    except discord.HTTPException:
        return None


class UnverifyModal(discord.ui.Modal, title="인증 초기화"):
    target = discord.ui.TextInput(
        label="해제할 유저", placeholder="멘션 또는 숫자 ID", required=True, max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        member = await _resolve_member(guild, str(self.target.value))
        if member is None:
            await interaction.response.send_message(
                "❌ 유저를 찾을 수 없어요. 멘션 또는 숫자 ID로 입력해주세요.", ephemeral=True
            )
            return
        verified = resolve_verified_role(guild)
        unverified = resolve_unverified_role(guild)
        await interaction.response.defer(ephemeral=True)
        try:
            if verified and verified in member.roles:
                await member.remove_roles(verified, reason="관리자 인증 초기화")
            if unverified and unverified not in member.roles:
                await member.add_roles(unverified, reason="관리자 인증 초기화")
        except discord.Forbidden:
            await interaction.followup.send("❌ 권한 부족 (봇 롤 순서 확인)", ephemeral=True)
            return
        await interaction.followup.send(
            f"{member.mention} 인증을 해제했어요.", ephemeral=True
        )
        await send_log(
            guild, f"🔄 **인증해제** {member.mention} (by {interaction.user.mention})"
        )


_WEBHOOK_TEST_PAYLOAD = {
    "embeds": [{
        "title": "🔗 인증 로그 연결됨",
        "description": "앞으로 인증 성공 시 여기에 로그가 전송됩니다.",
        "color": 0x57F287,
    }]
}


class WebhookModal(discord.ui.Modal, title="인증 로그 웹훅"):
    url = discord.ui.TextInput(
        label="웹훅 URL", placeholder="https://discord.com/api/webhooks/...",
        required=True, max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        url = str(self.url.value).strip()
        if not webverify.is_webhook_url(url):
            await interaction.followup.send(
                "❌ `https://discord.com/api/webhooks/...` 형태여야 해요.",
                ephemeral=True,
            )
            return
        # 테스트는 봇 프로세스에서 직접 보낸다.
        # Render IP 는 디스코드 웹훅 POST 제한(429)을 받아 웹쪽 검사는
        # 정상 주소도 항상 실패로 보이기 때문.
        ok, detail = await webverify.post_webhook(url, _WEBHOOK_TEST_PAYLOAD)
        saved = False
        if ok or webverify.is_transient_error(detail):
            try:
                saved = await webapi.webhook_save(guild.id, url)
            except webapi.ApiError as e:
                await interaction.followup.send(
                    f"{'✅' if ok else '⚠️'} 테스트는 "
                    f"{'성공했지만' if ok else '실패했지만'} 저장도 실패했어요: {e}",
                    ephemeral=True,
                )
                return
        short = " ".join(detail.split())[:160]
        if not ok:
            hint = ""
            if "429" in detail:
                hint = ("\n💡 429 = 디스코드가 웹훅 요청을 잠깐 제한한 거예요.\n"
                        "이번에는 **봇 쪽에서 직접 보냈는데도** 막힌 상태라 "
                        "수 분 뒤 다시 시도해보세요.")
            elif "404" in detail:
                hint = ("\n💡 404 = 웹훅이 삭제됐거나 URL 오타. "
                        "채널 설정 → 연동 → 웹훅에서 URL을 다시 복사해주세요.")
            elif "401" in detail or "403" in detail:
                hint = "\n💡 401/403 = 토큰이 유효하지 않음. 웹훅을 새로 만드세요."
            saved_note = (
                "\n💾 URL 은 저장해뒀어요. 실제 인증 때 봇이 대신 전송을 시도해요."
                if saved else ""
            )
            await interaction.followup.send(
                f"❌ 웹훅 전송 테스트 실패.\n상세: `{short}`{hint}{saved_note}",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "✅ 인증 로그 웹훅이 설정됐어요. 테스트 메시지를 보냈으니 확인해보세요.\n"
            "이제 로그는 **봇이 직접 전송**해요.",
            ephemeral=True,
        )


# ---------- 인증 로그 (/인증로그 → ephemeral 조작판) ----------
# 로그 설정을 /인증관리 에서 빼서 전용 명령으로 분리했다.
#   - 로그 채널: 봇이 **일반 메시지**로 직접 전송 → 디스코드 웹훅 429(IP 제한)와
#     상관없이 바로 도착한다. 로그가 늦거나 안 올 때는 이쪽을 쓰면 된다.
#   - 웹훅: 다른 서버까지 보내고 싶을 때만. 봇 IP 로 POST 한다.
# custom_id 고정 → bot.add_view 로 영구 유지. 채널 메시지에는 절대 붙지 않는다.


def verifylog_embed(guild: discord.Guild) -> discord.Embed:
    ch_id = get_verify_log_channel(guild.id)
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch_id and ch is None:
        ch_text = f"<#{ch_id}> (찾을 수 없음 — 다시 지정해주세요)"
    elif isinstance(ch, discord.TextChannel):
        ch_text = ch.mention
    else:
        ch_text = "❌ 미지정"
    embed = discord.Embed(
        title="📋 인증 로그 설정",
        description=(
            "로그채널이면 **봇이 직접 메시지**로 보내서 바로 도착해요.\n"
            "웹훅은 다른 서버까지 보내고 싶을 때만 등록하면 돼요."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="로그채널", value=ch_text, inline=True)
    embed.add_field(
        name="지금 상태",
        value="**[🔍 로그 상태]** 를 눌러 대기열/전송 결과를 확인하세요",
        inline=False,
    )
    return embed


class VerifyLogView(discord.ui.View):
    """인증 로그 관리 — /인증로그 로만 열리는 ephemeral 뷰.

    0행: 로그 채널 드롭다운 / 1행: 웹훅·상태·전송 버튼
    custom_id 고정 → bot.add_view 로 영구 유지.
    """

    def __init__(self, guild_id: int | None = None):
        super().__init__(timeout=None)
        sel = discord.ui.ChannelSelect(
            custom_id="verifylog:channel",
            channel_types=[discord.ChannelType.text],
            placeholder="인증 로그를 받을 채널 (봇이 직접 메시지로 보냄)",
            min_values=1, max_values=1, row=0,
        )
        sel.callback = self._on_channel
        self.add_item(sel)

        specs = [
            ("verifylog:webhook:set", "📜 웹훅 등록",
             discord.ButtonStyle.secondary, self._webhook_set, 1),
            ("verifylog:webhook:clear", "🗑 웹훅 해제",
             discord.ButtonStyle.red, self._webhook_clear, 1),
            ("verifylog:status", "🔍 로그 상태",
             discord.ButtonStyle.secondary, self._log_status, 1),
            ("verifylog:flush", "📤 지금 전송",
             discord.ButtonStyle.green, self._flush, 1),
            ("verifylog:queue:clear", "🗑 대기열 비우기",
             discord.ButtonStyle.red, self._clear_queue, 1),
        ]
        for cid, label, style, cb, row in specs:
            b = discord.ui.Button(custom_id=cid, label=label, style=style, row=row)
            b.callback = cb
            self.add_item(b)

    # --- 공통 ---
    # discord.py 는 item.callback(interaction) 만 넘긴다 → 값은 interaction.data 에서.
    @staticmethod
    def _picked(interaction: discord.Interaction) -> int | None:
        values = (interaction.data or {}).get("values") or []
        return int(values[0]) if values else None

    async def _refresh(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.edit_message(
            embed=verifylog_embed(guild), view=VerifyLogView(guild.id)
        )

    # --- 로그 채널 ---
    async def _on_channel(self, interaction: discord.Interaction) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        guild = interaction.guild
        rid = self._picked(interaction)
        if guild is None or rid is None:
            return
        ch = guild.get_channel(rid)
        if not isinstance(ch, discord.TextChannel):
            await _deny(interaction)
            return
        set_verify_log_channel(guild.id, ch.id)
        await self._refresh(interaction)
        await interaction.followup.send(
            f"✅ 인증 로그를 {ch.mention} 으로 보낼게요.\n"
            "봇이 직접 메시지를 보내므로 바로 도착해요.",
            ephemeral=True,
        )

    # --- 지금 전송 / 대기열 비우기 ---
    async def _flush(self, interaction: discord.Interaction) -> None:
        """쌓인 로그를 지금 바로 꺼내 전송한다 (테스트 겸용)."""
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            items = await webapi.queue_take()
        except webapi.ApiError as e:
            await interaction.followup.send(
                "❌ 웹(Render) 서버에 연결 못했어요: "
                f"`{' '.join(str(e).split())[:200]}`",
                ephemeral=True,
            )
            return
        sent = failed = 0
        for item in items:
            s, f = await _process_queue_item(item)
            sent += s
            failed += f
        if not items:
            await interaction.followup.send(
                "📭 대기열이 비어 있어요. 로그가 쌓이면 여기서 바로 보낼 수 있어요.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"📤 대기열 {len(items)}건 처리 → 전송 {sent} / 재시도 {failed}",
            ephemeral=True,
        )

    async def _clear_queue(self, interaction: discord.Interaction) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            n = await webapi.queue_clear()
        except webapi.ApiError as e:
            await interaction.followup.send(
                "❌ 웹(Render) 서버에 연결 못했어요: "
                f"`{' '.join(str(e).split())[:200]}`",
                ephemeral=True,
            )
            return
        await interaction.followup.send(f"🗑 대기열 {n}건을 비웠어요.", ephemeral=True)

    # --- 웹훅 ---
    async def _webhook_set(self, interaction: discord.Interaction) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        await interaction.response.send_modal(WebhookModal())

    async def _webhook_clear(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            removed = await webapi.webhook_remove(guild.id)
        except webapi.ApiError as e:
            await interaction.followup.send(f"❌ 웹 서버 연결 실패: {e}", ephemeral=True)
            return
        await interaction.followup.send(
            "✅ 인증 로그 웹훅을 해제했어요." if removed else "설정된 웹훅이 없어요.",
            ephemeral=True,
        )

    async def _log_status(self, interaction: discord.Interaction) -> None:
        """웹훅 등록 여부 / 대기열 적체 / 마지막 전송 결과를 한 번에 보여준다.

        로그가 안 올 때 "웹이 못 보냈나", "봇이 안 받아갔나", "웹훅이 아예 없나"
        를 한 번에 가를 수 있게 하는 진단 버튼.
        """
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            d = await webapi.web_status()
        except webapi.ApiError as e:
            await interaction.followup.send(
                "❌ 웹(Render) 서버에 연결 못했어요.\n"
                f"`{' '.join(str(e).split())[:200]}`\n"
                "→ Render 가 꺼져 있거나, 봇 환경변수 `WEB_PUBLIC_URL` / `WEB_SECRET` "
                "이 Render 쪽 값과 다르면 이럽니다.",
                ephemeral=True,
            )
            return
        hooks = int(d.get("webhooks", 0) or 0)
        queued = int(d.get("queued", 0) or 0)
        last = d.get("last") or {}
        lines = ["📋 **인증 로그 상태**", ""]
        lines.append(
            f"· 웹훅 등록: {'✅ ' + str(hooks) + '개' if hooks else '❌ 없음'}"
        )
        if not hooks:
            lines.append("  → **[📜 웹훅 등록]** 으로 URL 을 등록해주세요.")
        lines.append(f"· 대기열 적체: **{queued}개**")
        if queued:
            lines.append(
                "  → **봇 호스팅을 최신 코드로 올리고 재시작**해야 해요.\n"
                "  (봇이 대기열을 안 받아가면 로그가 계속 여기 쌓입니다)"
            )
        lines.append("")
        if last.get("ts"):
            when = datetime.fromtimestamp(int(last["ts"])).strftime("%H:%M:%S")
            ok = bool(last.get("ok"))
            lines.append(
                f"· 웹(Render) 마지막 전송: "
                f"{'✅ 성공' if ok else '❌ 실패'} (`{when}`)"
            )
            if last.get("detail"):
                lines.append(f"  - `{str(last['detail'])[:120]}`")
            if last.get("queued"):
                lines.append("  → 봇 대기열로 넘겼습니다.")
        else:
            lines.append(
                "· 웹(Render) 마지막 전송: 기록 없음 (웹 인증이 아직 한 번도 안 일어났음)"
            )
        if _LOG_PUMP_LAST.get("ts"):
            when = datetime.fromtimestamp(int(_LOG_PUMP_LAST["ts"])).strftime("%H:%M:%S")
            lines.append(
                f"· 봇 마지막 대기열 처리: 대상 {_LOG_PUMP_LAST.get('taken', 0)}개 → "
                f"성공 {_LOG_PUMP_LAST.get('sent', 0)} / "
                f"실패 {_LOG_PUMP_LAST.get('failed', 0)} (`{when}`)"
            )
            _detail = str(_LOG_PUMP_LAST.get("detail") or "")
            if _detail:
                lines.append(f"  - `{_detail[:120]}`")
            if "unauthorized" in _detail.lower():
                lines.append(
                    "  ⚠️ **401 거부** → 봇 Env 와 **Render Env 의 `WEB_SECRET` 이 다릅니다.**\n"
                    "     Render(웹) → Env 탭에서 `WEB_SECRET` 값 확인해서\n"
                    "     **Bot-Hosting Env의 `WEB_SECRET` 을 그 값으로 바꿔 저장**하면 끝나요."
                )
        else:
            lines.append("· 봇 마지막 대기열 처리: 아직 안 돌았어요 (재시작 후 5초~)")
        await interaction.followup.send("\n".join(lines), ephemeral=True)


    # (웹훅 버튼들은 /인증관리 에서 옮겨옴)


@tree.command(name="인증로그", description="인증 로그 설정 (로그채널 / 웹훅 / 전송 상태)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(채널="로그를 받을 채널. 비워두면 지금 설정 유지")
async def verify_log_panel(
    interaction: discord.Interaction,
    채널: discord.TextChannel | None = None,
):
    guild = interaction.guild
    assert guild is not None
    if 채널 is not None:
        set_verify_log_channel(guild.id, 채널.id)
    await interaction.response.send_message(
        embed=verifylog_embed(guild), view=VerifyLogView(guild.id), ephemeral=True
    )


class RecoverModal(discord.ui.Modal, title="복구"):
    key = discord.ui.TextInput(
        label="복구키", placeholder="XXXX-XXXX-XXXX", required=True, max_length=64,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        key = str(self.key.value).strip()
        gid = recover.key_to_guild(key)
        if gid is None:
            await interaction.response.send_message(
                "❌ 유효하지 않은 복구키입니다.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            n = await recover.backup_count(gid)
        except webapi.ApiError as e:
            await interaction.followup.send(f"❌ 웹 서버 연결 실패: {e}", ephemeral=True)
            return
        if n == 0:
            await interaction.followup.send(
                "❌ 이 복구키에 쌓인 인원이 없습니다.", ephemeral=True
            )
            return
        _PENDING_RECOVER[interaction.user.id] = key
        await interaction.followup.send(
            f"🔑 이 복구키에 **{n}명** 쌓여 있습니다.\n"
            f"지금 이 서버로 복구할까요? (예상 약 {n * 1.5 / 60:.0f}분)",
            view=RecoverConfirmView(),
            ephemeral=True,
        )


class RecoverConfirmView(discord.ui.View):
    """복구 실행 확인 버튼. 복구키는 _PENDING_RECOVER 에 유저별로 들어있다."""

    def __init__(self):
        super().__init__(timeout=None)
        b = discord.ui.Button(
            custom_id="verify:recover:go",
            label="▶️ 바로 실행",
            style=discord.ButtonStyle.green,
        )
        b.callback = self._on_go
        self.add_item(b)

    async def _on_go(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        key = _PENDING_RECOVER.get(interaction.user.id)
        if guild is None:
            return
        if key is None:
            await interaction.response.send_message(
                "❌ 복구키가 초기화됐어요. [♻️ 복구 실행] 을 다시 눌러주세요.", ephemeral=True
            )
            return
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        gid = recover.key_to_guild(key)
        if gid is None:
            await interaction.response.send_message(
                "❌ 유효하지 않은 복구키입니다.", ephemeral=True
            )
            return
        me = guild.me
        if me is None or not me.guild_permissions.create_instant_invite:
            await interaction.response.send_message(
                "❌ 봇에 **멤버 초대하기** 권한이 없습니다. 역할 설정을 확인해주세요.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            total = await recover.backup_count(gid)
        except webapi.ApiError as e:
            await interaction.followup.send(f"❌ 웹 서버 연결 실패: {e}", ephemeral=True)
            return
        if total == 0:
            await interaction.followup.send(
                "❌ 이 복구키에 쌓인 인원이 없습니다.", ephemeral=True
            )
            return
        msg = await interaction.followup.send(
            f"🔄 복구 시작: {total}명 (예상 약 {total * 1.5 / 60:.0f}분)…", ephemeral=True
        )
        last_edit = 0.0

        async def progress(done: int, total_n: int) -> None:
            nonlocal last_edit
            if done == total_n or done % 10 == 0 or time.time() - last_edit > 10:
                last_edit = time.time()
                try:
                    await msg.edit(content=f"🔄 복구 중… {done}/{total_n}")
                except discord.HTTPException:
                    pass

        res = await recover.run_restore(guild.id, gid, progress)
        summary = (
            f"✅ 복구 완료: {total}명 중\n"
            f"- 새로 초대: {res['ok']}명\n"
            f"- 이미 있음: {res['already']}명\n"
            f"- 토큰 만료/취소: {res['dead']}명 (재인증 필요)\n"
            f"- 실패: {res['fail']}명"
        )
        await msg.edit(content=summary)
        await send_log(guild, f"🔑 **복구 실행** (by {interaction.user.mention})\n{summary}")


class RecoverKeySetModal(discord.ui.Modal, title="복구키 직접 설정"):
    key = discord.ui.TextInput(
        label="사용할 복구키",
        placeholder="예: ALPHA-2026-01 (영문/숫자/하이픈, 4~64자)",
        required=True,
        min_length=4,
        max_length=64,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        raw = str(self.key.value).strip()
        if not recover.valid_key(raw):
            await interaction.response.send_message(
                "❌ 영문·숫자·하이픈만 쓸 수 있고 4~64자여야 해요.", ephemeral=True
            )
            return
        owner = recover.key_to_guild(raw)
        if owner is not None and owner != guild.id:
            await interaction.response.send_message(
                "❌ 이미 다른 서버가 쓰는 복구키예요. 다른 값을 정해주세요.",
                ephemeral=True,
            )
            return
        old = recover.get_key(guild.id)
        new = recover.set_key(guild.id, raw, interaction.user.id)
        await interaction.response.send_message(
            f"✅ 복구키를 `{new}` 로 바꿨어요."
            + (f"\n이전 키 `{old}` 는 더 이상 쓸 수 없어요." if old else "")
            + "\n쌓인 인원은 그대로 유지됩니다.",
            ephemeral=True,
        )


class RecoverKeyDeleteView(discord.ui.View):
    """복구키 삭제 확인 버튼 — 안내 메시지(ephemeral)에 붙는다."""

    def __init__(self):
        super().__init__(timeout=None)
        for cid, label, style, cb in (
            ("verify:recover:delete_go", "🗑 삭제", discord.ButtonStyle.red, self._go),
            ("verify:recover:delete_no", "취소", discord.ButtonStyle.secondary, self._no),
        ):
            b = discord.ui.Button(custom_id=cid, label=label, style=style)
            b.callback = cb
            self.add_item(b)

    async def _go(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        key = recover.get_key(guild.id)
        if key is None:
            await interaction.response.edit_message(
                content="🗑 이미 삭제된 키였어요.", embed=None, view=None
            )
            return
        recover.delete_key(guild.id)
        await interaction.response.edit_message(
            content=(
                f"🗑 복구키 `{key}` 를 삭제했어요.\n"
                f"쌓인 인원은 남아 있으니 나중에 [✏️ 키 설정] 으로 키를 다시 만들면 이어집니다."
            ),
            embed=None,
            view=None,
        )

    async def _no(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="취소했어요.", embed=None, view=None
        )


class VerifyPanelView(discord.ui.View):
    """유저용 인증 패널 — [인증하기] 링크 버튼 하나뿐.

    이 뷰가 붙은 메시지만 채널에 게시된다. 관리자 버튼은 VerifyAdminView 로
    완전히 떼어서 /인증관리 명령으로만 열기 때문에 유저는 절대 못 본다.
    링크 URL 은 패널마다 다르므로 게시할 때 넣는다.
    """

    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="인증하기", url=url))


def verify_admin_embed(guild: discord.Guild) -> discord.Embed:
    verified = resolve_verified_role(guild)
    unverified = resolve_unverified_role(guild)
    embed = discord.Embed(
        title="🛠️ 인증 관리",
        description="위 드롭다운으로 역할을 지정하고, 아래 버튼으로 인증을 관리해요.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="지급 역할", value=verified.mention if verified else "미지정", inline=True
    )
    embed.add_field(
        name="미인증 역할", value=unverified.mention if unverified else "미지정", inline=True
    )
    embed.add_field(
        name="패널 색상", value=f"**#{get_panel_color(guild.id):06X}**", inline=True
    )
    missing = [
        label
        for label, r in (("지급", verified), ("미인증", unverified))
        if r is None
    ]
    if missing:
        embed.add_field(
            name="⚠️ 역할을 아직 못 찾았어요",
            value=(
                f"**{'·'.join(missing)} 역할**이 미지정입니다.\n"
                "위 드롭다운에서 골라주면 바로 저장돼요.\n"
                "(.env 의 `VERIFIED_ROLE_NAME` / `UNVERIFIED_ROLE_NAME` 값은\n"
                "서버에 있는 실제 **역할 이름**과 빠짐없이 일치해야 해요)"
            ),
            inline=False,
        )
    return embed


async def _announce_panel_refresh(
    interaction: discord.Interaction,
    guild: discord.Guild,
    what: str,
    role: discord.Role | None = None,
) -> None:
    """게시된 인증 패널을 다시 쓰고, 몇 개 바꿨는지 알려준다.

    관리패널 편집은 바로 끝내고(3초 규칙) 이쪽은 뒤에서 천천히 돌린다.
    """
    try:
        ok, fail, missing = await refresh_verify_panels(guild, role)
    except Exception:
        log.exception("인증 패널 자동 갱신 실패")
        return
    bits = [f"🔄 게시된 인증 패널 **{ok}개**가 {what} 바뀌었어요."]
    if fail:
        bits.append(f"수정 실패 {fail}개 (봇 권한 확인)")
    if missing:
        bits.append(f"찾을 수 없음 {missing}개 (삭제된 패널)")
    try:
        await interaction.followup.send(" ".join(bits), ephemeral=True)
    except discord.HTTPException:
        pass


async def _apply_panel_color(
    interaction: discord.Interaction, guild: discord.Guild, value: int | None
) -> None:
    """패널 바 색상 저장 → 관리패널 갱신 → 채널에 게시된 패널에도 바로 반영."""
    set_panel_color(guild.id, value)
    shown = get_panel_color(guild.id)
    await interaction.response.edit_message(
        embed=verify_admin_embed(guild), view=VerifyAdminView()
    )
    await _announce_panel_refresh(
        interaction, guild, f"색상 `#{shown:06X}`(으)로", resolve_verified_role(guild)
    )


class PanelColorModal(discord.ui.Modal, title="인증 패널 색상"):
    color = discord.ui.TextInput(
        label="색상 (HEX 또는 색 이름)",
        placeholder="예: #57F287 · 그린 · 블루 · 1ABC9C · 기본",
        required=True,
        max_length=32,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        raw = str(self.color.value).strip()
        if raw in ("기본", "없음", "초기화", "default", "none", "reset"):
            value = None
        else:
            value = parse_color(raw)
            if value is None:
                await interaction.response.send_message(
                    "❌ 색상을 못 읽어요. `#57F287` 처럼 HEX 6자리, "
                    "또는 그린/블루/레드 같은 색 이름으로 적어주세요.",
                    ephemeral=True,
                )
                return
        await _apply_panel_color(interaction, guild, value)


class VerifyAdminView(discord.ui.View):
    """인증 관리 패널 — /인증관리 명령으로만 열리는 ephemeral 뷰.

    위 두 줄(RoleSelect)으로 지급/미인증 역할을 그자리에서 지정하고,
    버튼으로 인증 초기화·웹훅·복구키·패널 색상을 다룬다.
    (마지막 줄 = 패널 왼쪽 바 색상 프리셋/직접 입력)
    custom_id 고정 → bot.add_view 로 영구 유지.
    채널에 게시되는 메시지에는 절대 붙지 않으므로 유저는 볼 수 없다.
    """

    def __init__(self):
        super().__init__(timeout=None)

        vs = discord.ui.RoleSelect(
            custom_id="verify:role:verified",
            placeholder="지급 역할 — 인증 완료자가 받는 역할",
            min_values=1, max_values=1, row=0,
        )
        vs.callback = self._on_verified_role
        self.add_item(vs)

        us = discord.ui.RoleSelect(
            custom_id="verify:role:unverified",
            placeholder="미인증 역할 — 입장 시 먼저 받는 역할",
            min_values=1, max_values=1, row=1,
        )
        us.callback = self._on_unverified_role
        self.add_item(us)

        specs = [
            ("verify:admin:reset", "🔄 인증 초기화",
             discord.ButtonStyle.secondary, self._reset, 2),
            ("verify:recover:key", "🔑 복구키 확인",
             discord.ButtonStyle.secondary, self._recover_key, 3),
            ("verify:recover:set", "✏️ 키 설정",
             discord.ButtonStyle.secondary, self._recover_key_set, 3),
            ("verify:recover:delete", "🗑 키 삭제",
             discord.ButtonStyle.red, self._recover_key_delete, 3),
            ("verify:recover:run", "♻️ 복구 실행",
             discord.ButtonStyle.green, self._recover_run, 3),
            # 4행: 패널 왼쪽 바 색상
            ("verify:color:open", "🎨 색상 설정",
             discord.ButtonStyle.secondary, self._color_open, 4),
            ("verify:color:green", "🟢 그린",
             discord.ButtonStyle.success, self._color_green, 4),
            ("verify:color:blue", "🔵 블루",
             discord.ButtonStyle.primary, self._color_blue, 4),
            ("verify:color:purple", "🟣 퍼플",
             discord.ButtonStyle.primary, self._color_purple, 4),
            ("verify:color:red", "🔴 레드",
             discord.ButtonStyle.danger, self._color_red, 4),
        ]
        for cid, label, style, cb, row in specs:
            b = discord.ui.Button(custom_id=cid, label=label, style=style, row=row)
            b.callback = cb
            self.add_item(b)

    # --- 역할 지정 (RoleSelect) ---
    # discord.py 는 item.callback(interaction) 만 넘긴다 → 값은 interaction.data 에서.
    @staticmethod
    def _picked(interaction: discord.Interaction) -> int | None:
        values = (interaction.data or {}).get("values") or []
        return int(values[0]) if values else None

    async def _set_role(
        self, interaction: discord.Interaction, kind: str, must_be_grantable: bool
    ) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        guild = interaction.guild
        rid = self._picked(interaction)
        if guild is None or rid is None:
            return
        role = guild.get_role(rid)
        if role is None:
            await _deny(interaction)
            return
        me = guild.me
        if must_be_grantable and me and me.top_role <= role:
            await interaction.response.send_message(
                f"❌ {role.mention}이(가) 봇 롤보다 높아서 지급할 수 없어요.\n"
                f"서버 설정 → 역할에서 **봇 롤을 위로** 올려주세요.",
                ephemeral=True,
            )
            return
        set_guild_role(guild.id, kind, role.id)
        await interaction.response.edit_message(
            embed=verify_admin_embed(guild), view=VerifyAdminView()
        )
        if kind == "verified":
            await _announce_panel_refresh(
                interaction, guild, f"새 역할({role.mention})으로", role
            )

    async def _on_verified_role(self, interaction: discord.Interaction) -> None:
        await self._set_role(interaction, "verified", True)

    async def _on_unverified_role(self, interaction: discord.Interaction) -> None:
        await self._set_role(interaction, "unverified", False)

    # --- 패널 왼쪽 바 색상 ---
    async def _set_color(self, interaction: discord.Interaction, value: int) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        guild = interaction.guild
        if guild is None:
            return
        await _apply_panel_color(interaction, guild, value)

    async def _color_open(self, interaction: discord.Interaction) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        await interaction.response.send_modal(PanelColorModal())

    async def _color_green(self, interaction: discord.Interaction) -> None:
        await self._set_color(interaction, COLOR_NAMES["green"])

    async def _color_blue(self, interaction: discord.Interaction) -> None:
        await self._set_color(interaction, COLOR_NAMES["blue"])

    async def _color_purple(self, interaction: discord.Interaction) -> None:
        await self._set_color(interaction, COLOR_NAMES["purple"])

    async def _color_red(self, interaction: discord.Interaction) -> None:
        await self._set_color(interaction, COLOR_NAMES["red"])

    async def _reset(self, interaction: discord.Interaction) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        await interaction.response.send_modal(UnverifyModal())

    async def _recover_key(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        key, is_new = recover.ensure_key(guild.id, interaction.user.id)
        text = (
            f"🔑 **{guild.name} 서버의 복구키**\n`{key}`\n\n"
            f"• 이 서버에서 웹 인증하는 사람이 이 키 앞으로 자동 누적됩니다.\n"
            f"• 테러 후 새 서버에서 [♻️ 복구 실행] → 멤버 재초대\n"
            f"⚠️ 절대 공유하지 마세요!"
        )
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.user.send(text)
            await interaction.followup.send(
                f"✅ 복구키를 DM으로 보냈어요.{' (새로 발급됨)' if is_new else ''}",
                ephemeral=True,
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                f"⚠️ DM이 막혀 있어 여기에 표시합니다. 확인 후 이 메시지를 지우세요.\n{text}",
                ephemeral=True,
            )

    async def _recover_run(self, interaction: discord.Interaction) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        await interaction.response.send_modal(RecoverModal())

    async def _recover_key_set(self, interaction: discord.Interaction) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        await interaction.response.send_modal(RecoverKeySetModal())

    async def _recover_key_delete(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        key = recover.get_key(guild.id)
        if key is None:
            await interaction.response.send_message(
                "ℹ️ 이 서버엔 등록된 복구키가 없어요.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"⚠️ 복구키 `{key}` 를 삭제할까요?\n"
            f"• 그 키로는 더 이상 복구할 수 없어요.\n"
            f"• 쌓인 인원은 남아 있으니 나중에 [✏️ 키 설정] 으로 키를 다시 만들면 그대로 이어져요.",
            view=RecoverKeyDeleteView(),
            ephemeral=True,
        )


# 뷰 클래스가 아래에 정의돼서 위쪽 add_view 블록에 못 넣는다 → 여기서 등록
bot.add_view(VerifyAdminView())
bot.add_view(VerifyLogView())
bot.add_view(RecoverConfirmView())
bot.add_view(RecoverKeyDeleteView())




# ---------- 슬래시 명령: 티켓 ----------
@tree.command(name="티켓패널", description="티켓 패널 게시 (버튼 → 전용 티켓 채널)")
@app_commands.describe(
    채널="유저용 티켓 패널을 올릴 텍스트 채널 (비우면 현재 채널)",
    제목="패널 제목 (비우면 기본값)",
    설명="패널 안내 문구 (비우면 기본값)",
    역할="티켓 스태프 역할 (지정하면 티켓 설정에도 저장)",
    카테고리="티켓이 생성될 카테고리 (지정하면 티켓 설정에도 저장)",
    색상="패널 왼쪽 바 색상 (예: #6366F1 또는 인디고, 비우면 현재 설정 유지)",
    버튼="패널에 보여줄 버튼 (비우면 현재 설정 유지)",
)
@app_commands.choices(
    버튼=[
        app_commands.Choice(name="둘 다", value="both"),
        app_commands.Choice(name="💳 결제 문의만", value="purchase"),
        app_commands.Choice(name="💬 일반·파트너 문의만", value="general"),
    ]
)
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_panel(
    interaction: discord.Interaction,
    채널: discord.abc.GuildChannel | None = None,
    제목: str | None = None,
    설명: str | None = None,
    역할: discord.Role | None = None,
    카테고리: discord.abc.GuildChannel | None = None,
    색상: str | None = None,
    버튼: str | None = None,
):
    target = 채널 or interaction.channel
    if isinstance(target, discord.Thread):
        target = target.parent
    if not isinstance(target, discord.TextChannel):
        got = getattr(target, "mention", "선택한 항목")
        await interaction.response.send_message(
            f"❌ {got}에는 패널을 올릴 수 없어요.\n"
            f"**채널** 칸에는 일반 **텍스트 채널**을 골라주세요.\n"
            f"비워두면 명령을 입력한 현재 채널에 게시됩니다.",
            ephemeral=True,
        )
        return
    guild = interaction.guild
    assert guild is not None
    if 카테고리 is not None and not isinstance(카테고리, discord.CategoryChannel):
        await interaction.response.send_message(
            "❌ **카테고리** 칸에는 카테고리만 골라주세요. (채널/음성 불가)",
            ephemeral=True,
        )
        return
    if 색상 is not None:
        value = parse_color(색상)
        if value is None:
            await interaction.response.send_message(
                f"❌ 색상을 못 읽어요. {HEX_HINT}로 적어주세요.", ephemeral=True
            )
            return
        ticket.set_color(guild.id, value)
    if 버튼 is not None:
        ticket.set_button_mode(guild.id, 버튼)
    updates = {}
    if 역할 is not None:
        updates["staff"] = 역할.id
    if 카테고리 is not None:
        updates["category"] = 카테고리.id
    if updates:
        ticket.update_settings(guild.id, **updates)
    await interaction.response.defer(ephemeral=True)
    try:
        msg = await target.send(
            embed=ticket.build_panel(제목, 설명, guild.id),
            view=ticket.PanelView(ticket.button_mode(guild.id)),
        )
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ 패널 게시 실패: {e}", ephemeral=True)
        return
    ticket.save_panel(msg.id, guild.id, target.id)
    await interaction.followup.send(
        f"{target.mention}에 티켓 패널을 올렸어요. ✅\n"
        f"{ticket.settings_summary(guild.id)}\n"
        f"관리 메뉴는 **/티켓관리** 로 열 수 있어요.",
        ephemeral=True,
    )


@tree.command(name="티켓관리", description="티켓 설정 조작판 (관리자, 본인 화면에만 뜹니다)")
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_admin_panel(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    await interaction.response.send_message(
        embed=ticket.admin_embed(guild.id), view=ticket.TicketAdminView(),
        ephemeral=True,
    )


# 티켓설정 / 티켓사유 / 티켓닫기 명령은 제거 →
#   설정·사유  : /티켓관리 (ephemeral 조작판)
#   티켓 닫기  : 티켓 채널 안의 [닫기 🔒] 버튼


async def _open_log_panel(
    interaction: discord.Interaction,
    kind: str,
    로그채널: discord.abc.GuildChannel | None,
):
    """입장/퇴장 로그 관리 패널 — 게시하지 않고 본인 화면에만 띄운다."""
    guild = interaction.guild
    assert guild is not None

    if 로그채널 is not None:
        target = 로그채널.parent if isinstance(로그채널, discord.Thread) else 로그채널
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "❌ **로그채널** 칸에는 일반 텍스트 채널만 골라주세요.", ephemeral=True
            )
            return
        joinleave.update(guild.id, kind, channel=target.id, enabled=True)

    await interaction.response.send_message(
        embed=joinleave.panel_embed(guild, kind),
        view=joinleave.LogPanelView(kind, guild.id),
        ephemeral=True,
    )


@tree.command(
    name="입장로그패널", description="입장 로그 관리 패널 (관리자, 본인 화면에만 뜹니다)"
)
@app_commands.describe(로그채널="입장 로그를 보낼 채널 (지정하면 바로 설정+켜기)")
@app_commands.checks.has_permissions(manage_guild=True)
async def join_log_panel(
    interaction: discord.Interaction,
    로그채널: discord.abc.GuildChannel | None = None,
):
    await _open_log_panel(interaction, "join", 로그채널)


@tree.command(
    name="퇴장로그패널", description="퇴장 로그 관리 패널 (관리자, 본인 화면에만 뜹니다)"
)
@app_commands.describe(로그채널="퇴장 로그를 보낼 채널 (지정하면 바로 설정+켜기)")
@app_commands.checks.has_permissions(manage_guild=True)
async def leave_log_panel(
    interaction: discord.Interaction,
    로그채널: discord.abc.GuildChannel | None = None,
):
    await _open_log_panel(interaction, "leave", 로그채널)


# ---------- 슬래시 명령: 오류 자동 보고 ----------
def err_report_embed(guild: discord.Guild) -> discord.Embed:
    ch_id = get_err_channel(guild.id)
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch_id and ch is None:
        ch_text = f"<#{ch_id}> (찾을 수 없음 — 다시 지정해주세요)"
    elif isinstance(ch, discord.TextChannel):
        ch_text = ch.mention
    else:
        ch_text = "❌ 미지정"
    e = discord.Embed(
        title="🚨 오류 자동 보고",
        description=(
            "봇에서 **오류가 발생하면** 이 채널로 자동 전송돼요.\n"
            "어느 명령에서 · 어느 줄에서 터졌는지 스택과 함께 옵니다.\n"
            "호스팅 콘솔은 글자를 못 읽으니 **여기가 확인할 수 있는 유일한 창구**입니다."
        ),
        color=discord.Color.red(),
    )
    e.add_field(name="보고 채널", value=ch_text, inline=False)
    e.add_field(
        name="지금까지",
        value=(
            f"전송 **{_ERR_STATE['sent']}건** · 중복 억제 **{_ERR_STATE['suppressed']}건**\n"
            f"마지막 전송 오류: `{_ERR_STATE['last_error'] or '-'}`"
        ),
        inline=False,
    )
    e.add_field(
        name="동작 규칙",
        value="같은 오류는 60초에 1회만 · 최소 3초 간격 (스팸·rate limit 방지)",
        inline=False,
    )
    return e


class ErrReportView(discord.ui.View):
    """오류 보고 설정 — /오류보고 로만 열리는 ephemeral 조작판.

    custom_id 고정 → bot.add_view 로 재시작 후에도 유지된다.
    """

    def __init__(self, guild_id: int | None = None):
        super().__init__(timeout=None)
        sel = discord.ui.ChannelSelect(
            custom_id="errlog:channel",
            channel_types=[discord.ChannelType.text],
            placeholder="봇 오류를 받을 채널",
            min_values=1,
            max_values=1,
            row=0,
        )
        sel.callback = self._on_channel
        self.add_item(sel)
        specs = (
            ("errlog:test", "🧪 오류 보고 테스트", discord.ButtonStyle.green, self._on_test),
            ("errlog:clear", "🗑 설정 해제", discord.ButtonStyle.red, self._on_clear),
        )
        for cid, label, style, cb in specs:
            b = discord.ui.Button(custom_id=cid, label=label, style=style, row=1)
            b.callback = cb
            self.add_item(b)

    # discord.py 는 item.callback(interaction) 만 넘긴다 → 값은 interaction.data 에서.
    @staticmethod
    def _picked(interaction: discord.Interaction) -> int | None:
        values = (interaction.data or {}).get("values") or []
        return int(values[0]) if values else None

    async def _refresh(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.edit_message(
            embed=err_report_embed(guild), view=ErrReportView(guild.id)
        )

    async def _on_channel(self, interaction: discord.Interaction) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        guild = interaction.guild
        cid = self._picked(interaction)
        if guild is None or cid is None:
            return
        ch = guild.get_channel(cid)
        if not isinstance(ch, discord.TextChannel):
            await _deny(interaction)
            return
        set_err_channel(guild.id, ch.id)
        await self._refresh(interaction)
        await interaction.followup.send(
            f"✅ 봇 오류가 나면 {ch.mention} 으로 자동 보고할게요.",
            ephemeral=True,
        )

    async def _on_clear(self, interaction: discord.Interaction) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        guild = interaction.guild
        if guild is None:
            return
        set_err_channel(guild.id, None)
        await self._refresh(interaction)
        await interaction.followup.send("🗑 오류 자동 보고를 껐어요.", ephemeral=True)

    async def _on_test(self, interaction: discord.Interaction) -> None:
        """의도적으로 오류 하나를 만들어 보고 경로 전체(수집→큐→전송)를 검증한다."""
        global _ERR_CTX_GUILD
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        guild = interaction.guild
        if guild is None:
            return
        if not get_err_channel(guild.id):
            await interaction.response.send_message(
                "⚠️ 먼저 위에서 보고 채널을 지정해주세요.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        _ERR_CTX_GUILD = guild.id
        try:
            raise RuntimeError("오류 보고 테스트 (이 오류는 의도적입니다)")
        except RuntimeError:
            log.exception("오류 보고 테스트")
        _ERR_CTX_GUILD = None
        await interaction.followup.send(
            "🧪 오류를 일부러 발생시켰어요. 지정한 채널에 들어갔는지 확인해주세요.\n"
            "*(수집 2초 + 전송 간격 3초 → 최대 5초 정도 걸려요)*",
            ephemeral=True,
        )


bot.add_view(ErrReportView())


@tree.command(
    name="오류보고",
    description="봇 오류 자동 보고 채널 설정 (관리자, 본인 화면에만 뜹니다)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def error_report_panel(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    await interaction.response.send_message(
        embed=err_report_embed(guild), view=ErrReportView(guild.id), ephemeral=True
    )


# ---------- 허니팟 (허수아비 채널) ----------
# 지정한 채널에 메시지를 보내면 → **바로 지우고** 보낸 사람을 제재한다.
# 강퇴 / 타임아웃 / 차단 중 하나를 고르고, 타임아웃이면 기간도 정한다.
#
# 메시지 **내용**은 아예 읽지 않는다 (누가 어느 채널에 보냈는지만 알면 되므로
# message_content 특권 인텐트 없이도 정상 동작 — 포털 설정을 건드리지 않는다).

HONEYPOT_ACTIONS = ("kick", "timeout", "ban")
HONEYPOT_ACTION_LABELS = {"kick": "🚪 강퇴", "timeout": "⏱ 타임아웃", "ban": "⛔ 차단"}
# 채널 경고 메시지에 들어갈 누적 표기 (제재 방식에 맞춰 문구도 바뀐다)
HONEYPOT_COUNT_LABELS = {
    "kick": "🚪 강퇴된 계정",
    "timeout": "⏱ 타임아웃된 계정",
    "ban": "⛔ 차단된 계정",
}
# 감시 채널에 띄워두는 안내 본문.
# '# 제목' 헤드라인 마크다운은 임베드가 아니라 메시지 본문에서만 지원된다 —
# 임베드 제목은 작아서 잘 안 보이므로 본문으로 빼서 크게 쓴다.
HONEYPOT_NOTICE_TEXT = "# 🍯 허니팟\n**⚠️ 이 채널에 채팅을 입력하지 마세요**"
# 타임아웃 기간(분). 디스코드 상한 = 28일 (40320분)
HONEYPOT_DURATIONS = (10, 30, 60, 360, 1440, 10080, 40320)
HONEYPOT_MAX_MINUTES = 28 * 24 * 60


def _hp_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _honeypot_minutes(value) -> int:
    v = _hp_int(value)
    if v is None:
        v = 60
    return max(1, min(HONEYPOT_MAX_MINUTES, v))


def fmt_minutes(minutes) -> str:
    m = _honeypot_minutes(minutes)
    if m % 1440 == 0:
        return f"{m // 1440}일"
    if m % 60 == 0:
        return f"{m // 60}시간"
    return f"{m}분"


def get_honeypot(guild_id: int) -> dict:
    """허니팟 설정. 값이 비어있어도 항상 쓸 수 있는 형태로 돌려준다."""
    raw = get_guild_roles(guild_id).get("honeypot")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    cfg["channel"] = _hp_int(cfg.get("channel"))
    if cfg.get("action") not in HONEYPOT_ACTIONS:
        cfg["action"] = "timeout"
    cfg["minutes"] = _honeypot_minutes(cfg.get("minutes", 60))
    cfg["hits"] = _hp_int(cfg.get("hits", 0)) or 0
    cfg["msg_id"] = _hp_int(cfg.get("msg_id"))  # 함정 메시지(카운터)의 ID
    return cfg


def set_honeypot(guild_id: int, **fields) -> dict:
    cfg = get_honeypot(guild_id)
    for key in ("channel", "action", "minutes", "hits", "msg_id"):
        if key in fields:
            cfg[key] = fields[key]
    if cfg.get("action") not in HONEYPOT_ACTIONS:
        cfg["action"] = "timeout"
    cfg["channel"] = _hp_int(cfg.get("channel"))
    cfg["minutes"] = _honeypot_minutes(cfg.get("minutes"))
    cfg["hits"] = _hp_int(cfg.get("hits")) or 0
    cfg["msg_id"] = _hp_int(cfg.get("msg_id"))
    _put_guild_setting(guild_id, "honeypot", cfg)
    return cfg


def honeypot_counter_view(cfg: dict) -> discord.ui.View:
    """제재 횟수를 담은 버튼 — 눌리지 않도록 disabled 로 만든다 (표시용일 뿐).

    임베드 필드보다 눈에 잘 들어오고, 숫자가 올라갈 때마다 같이 바뀐다.
    """
    action = cfg.get("action", "timeout")
    v = discord.ui.View()
    v.add_item(discord.ui.Button(
        label=f"{HONEYPOT_COUNT_LABELS.get(action, action)} · {cfg.get('hits') or 0}명",
        style=discord.ButtonStyle.secondary,
        disabled=True,
    ))
    return v


async def sync_trap_message(guild: discord.Guild) -> None:
    """감시 채널의 카운터 메시지를 설정에 맞춰 유지한다.

    없으면 새로 올리고, 있으면 내용만 고쳐서 숫자가 올라간 것처럼 보이게 한다.
    (매 적발마다 메시지를 새로 보내면 채널이 쓰레기로 차니까 — 메시지 하나를 계속 고친다)
    """
    cfg = get_honeypot(guild.id)
    ch_id = cfg.get("channel")
    ch = guild.get_channel(ch_id) if ch_id else None
    if not isinstance(ch, discord.TextChannel):
        return
    view = honeypot_counter_view(cfg)
    msg_id = cfg.get("msg_id")
    if msg_id:
        try:
            old = await ch.fetch_message(msg_id)
        except discord.NotFound:
            msg_id = None  # 지워져 있으면 새로 올린다
        except discord.HTTPException as e:
            log.warning("허니팟 메시지 조회 실패 ch=%s err=%s", ch_id, e)
            return
        else:
            try:
                # embed=None → 옛날 임베드 형태였다면 함께 없앤다
                await old.edit(content=HONEYPOT_NOTICE_TEXT, embed=None, view=view)
            except discord.HTTPException as e:
                log.warning("허니팟 메시지 수정 실패 err=%s", e)
            return
    try:
        sent = await ch.send(content=HONEYPOT_NOTICE_TEXT, view=view)
    except discord.HTTPException as e:
        log.warning("허니팟 메시지 전송 실패 ch=%s err=%s", ch_id, e)
        return
    set_honeypot(guild.id, msg_id=sent.id)


async def remove_trap_message(guild: discord.Guild) -> None:
    """감시 채널의 카운터 메시지를 지운다 (채널 바꾸거나 감시를 끌 때)."""
    cfg = get_honeypot(guild.id)
    ch_id = cfg.get("channel")
    msg_id = cfg.get("msg_id")
    ch = guild.get_channel(ch_id) if ch_id else None
    if msg_id and isinstance(ch, discord.TextChannel):
        try:
            await (await ch.fetch_message(msg_id)).delete()
        except discord.HTTPException:
            pass  # 이미 지워졌거나 권한이 없으면 그냥 넘어간다
    set_honeypot(guild.id, msg_id=None)


async def _honeypot_bust(message: discord.Message, cfg: dict) -> None:
    """메시지를 지우고 보낸 사람을 제재한다. 실패해도 루프는 살려둔다."""
    guild = message.guild
    member = message.author
    assert guild is not None
    action = cfg.get("action", "timeout")
    minutes = _honeypot_minutes(cfg.get("minutes", 60))
    reason = "허니팟 채널에 메시지를 보냄"

    # 1) 메시지부터 지운다 — 남아있으면 안 된다
    try:
        await message.delete()
    except discord.HTTPException as e:
        log.warning("허니팟 메시지 삭제 실패 ch=%s err=%s",
                    getattr(getattr(message, "channel", None), "id", "?"), e)

    # 2) 제재
    err = ""
    try:
        if action == "kick":
            await member.kick(reason=reason)
        elif action == "ban":
            await guild.ban(member, reason=reason)
        else:
            await member.timeout(timedelta(minutes=minutes), reason=reason)
    except discord.Forbidden:
        err = "권한 부족 — 봇 역할이 대상보다 위에 있어야 하고 강퇴/차단 권한이 필요해요."
    except discord.HTTPException as e:
        err = f"디스코드 응답 오류 HTTP {e.status}"

    # 3) 적발 누적 — 실제로 제재가 먹힌 경우에만 센다 (권한 문제로 실패한 건 횟수에 넣지 않는다)
    hits = (cfg.get("hits") or 0) + (0 if err else 1)
    if not err:
        try:
            set_honeypot(guild.id, hits=hits)
        except Exception:
            pass

    # 4) 채널의 카운터 메시지 숫자만 올린다 — 매번 새로 보내지 않는다.
    #    (그 밖의 로그 채널 전송은 하지 않는다. 강퇴/차단/타임아웃은 디스코드 감사 로그에 자동으로 남는다)
    try:
        await sync_trap_message(guild)
    except discord.HTTPException as e2:
        log.warning("허니팟 카운터 갱신 실패 err=%s", e2)
    if err:
        log.warning("허니팟 제재 실패 guild=%s action=%s: %s", guild.id, action, err)


@bot.event
async def on_message(message: discord.Message) -> None:
    """허니팟 감지. 메시지 내용은 보지 않고 채널과 발신자만 본다."""
    if message.guild is None:
        return
    if getattr(message, "webhook_id", None):
        return
    author = message.author
    if getattr(author, "bot", False):
        return
    cfg = get_honeypot(message.guild.id)
    channel_id = cfg.get("channel")
    if not channel_id or channel_id != getattr(message.channel, "id", None):
        return
    if not isinstance(author, discord.Member):
        return
    perms = author.guild_permissions
    if perms.administrator or perms.manage_guild or perms.manage_messages:
        return  # 관리자·스태프는 유인 채널을 써도 제재하지 않는다
    await _honeypot_bust(message, cfg)


def honeypot_embed(guild: discord.Guild) -> discord.Embed:
    cfg = get_honeypot(guild.id)
    ch_id = cfg.get("channel")
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch_id and ch is None:
        ch_text = f"<#{ch_id}> (찾을 수 없음 — 다시 지정해주세요)"
        state = "⚠️ 채널 사라짐"
        color = discord.Color.orange()
    elif ch is not None:
        ch_text = ch.mention
        state = "🟢 켜짐"
        color = discord.Color.red()
    else:
        ch_text = "❌ 미지정 — **꺼짐**"
        state = "🔴 꺼짐"
        color = discord.Color.blurple()
    e = discord.Embed(
        title="🍯 허니팟",
        description=(
            "채널을 지정하면 그 채널에 **카운터 메시지**를 올려두고, 누가 입력하면 "
            "**바로 지우고** 제재해요. 잡은 사람 숫자가 그 메시지에 그대로 올라가요.\n"
            "메시지 내용은 읽지 않아도 동작해서 별도 인텐트 설정이 필요 없어요. "
            "관리자·스태프는 자동 제외."
        ),
        color=color,
    )
    e.add_field(name="상태", value=state, inline=True)
    e.add_field(name="대상 채널", value=ch_text, inline=False)
    e.add_field(name="제재 방식", value=HONEYPOT_ACTION_LABELS[cfg["action"]], inline=True)
    e.add_field(name="타임아웃 기간", value=fmt_minutes(cfg["minutes"]), inline=True)
    e.add_field(name="누적 적발", value=f"{cfg['hits']}명", inline=True)
    e.add_field(name="카운터 메시지",
                value="🟢 올려짐" if cfg.get("msg_id") else "❌ 없음",
                inline=True)
    return e


class HoneypotView(discord.ui.View):
    """허니팟 설정 — /허니팟 으로만 열리는 ephemeral 조작판.

    custom_id 고정 + timeout=None → bot.add_view 로 재시작 후에도 유지된다.
    """

    def __init__(self, guild_id: int | None = None):
        super().__init__(timeout=None)
        cfg = get_honeypot(guild_id) if guild_id else {}

        sel = discord.ui.ChannelSelect(
            custom_id="honeypot:channel",
            channel_types=[discord.ChannelType.text],
            placeholder="감시할 채널 지정",
            min_values=1,
            max_values=1,
            row=0,
        )
        sel.callback = self._on_channel
        self.add_item(sel)

        for cid, label, act, cb in (
            ("honeypot:kick", "🚪 강퇴", "kick", self._on_kick),
            ("honeypot:timeout", "⏱ 타임아웃", "timeout", self._on_timeout),
            ("honeypot:ban", "⛔ 차단", "ban", self._on_ban),
        ):
            style = (
                discord.ButtonStyle.primary
                if cfg.get("action") == act
                else discord.ButtonStyle.secondary
            )
            b = discord.ui.Button(custom_id=cid, label=label, style=style, row=1)
            b.callback = cb
            self.add_item(b)

        # 2행: 타임아웃 기간 (지금 값을 ✔ 로 표시)
        dur = discord.ui.Select(
            custom_id="honeypot:duration",
            placeholder="타임아웃 기간",
            min_values=1,
            max_values=1,
            row=2,
        )
        cur = int(cfg.get("minutes", 60)) if cfg else 60
        options = list(HONEYPOT_DURATIONS)
        if cur not in options:
            options.insert(0, cur)
        for m in options:
            dur.append_option(
                discord.SelectOption(
                    label=(f"✔️ {fmt_minutes(m)}" if m == cur else fmt_minutes(m)),
                    value=str(m),
                )
            )
        dur.callback = self._on_duration
        self.add_item(dur)

        clear = discord.ui.Button(
            custom_id="honeypot:clear",
            label="🔕 감시 해제",
            style=discord.ButtonStyle.danger,
            row=3,
        )
        clear.callback = self._on_clear
        self.add_item(clear)

    # discord.py 는 item.callback(interaction) 만 넘긴다 → 값은 interaction.data 에서
    @staticmethod
    def _picked(interaction: discord.Interaction) -> int | None:
        values = (interaction.data or {}).get("values") or []
        return int(values[0]) if values else None

    async def _refresh(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.edit_message(
            embed=honeypot_embed(guild), view=HoneypotView(guild.id)
        )

    async def _on_channel(self, interaction: discord.Interaction) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        guild = interaction.guild
        cid = self._picked(interaction)
        if guild is None or cid is None:
            return
        ch = guild.get_channel(cid)
        if not isinstance(ch, discord.TextChannel):
            await _deny(interaction)
            return
        await remove_trap_message(guild)  # 이전 채널의 카운터를 지운다
        set_honeypot(guild.id, channel=ch.id, msg_id=None)
        await sync_trap_message(guild)  # 새 채널에 카운터를 올려둔다
        await self._refresh(interaction)
        await interaction.followup.send(
            f"🍯 {ch.mention} 채널에 카운터를 올렸어요. "
            "누가 입력하면 **즉시 삭제 + 제재**되고 숫자가 올라가요.",
            ephemeral=True,
        )

    async def _set_action(self, interaction: discord.Interaction, action: str) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        guild = interaction.guild
        if guild is None:
            return
        set_honeypot(guild.id, action=action)
        await sync_trap_message(guild)  # 카운터 문구도 제재 방식에 맞춰 바꾼다
        await self._refresh(interaction)
        await interaction.followup.send(
            f"✅ 제재 방식을 **{HONEYPOT_ACTION_LABELS[action]}**(으)로 바꿨어요.",
            ephemeral=True,
        )

    async def _on_kick(self, interaction: discord.Interaction) -> None:
        await self._set_action(interaction, "kick")

    async def _on_timeout(self, interaction: discord.Interaction) -> None:
        await self._set_action(interaction, "timeout")

    async def _on_ban(self, interaction: discord.Interaction) -> None:
        await self._set_action(interaction, "ban")

    async def _on_duration(self, interaction: discord.Interaction) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        guild = interaction.guild
        picked = self._picked(interaction)
        if guild is None or picked is None:
            return
        set_honeypot(guild.id, minutes=picked)
        await self._refresh(interaction)
        await interaction.followup.send(
            f"⏱ 타임아웃 기간을 **{fmt_minutes(picked)}**로 맞췄어요.", ephemeral=True
        )

    async def _on_clear(self, interaction: discord.Interaction) -> None:
        if not _verify_admin_ok(interaction):
            await _deny(interaction)
            return
        guild = interaction.guild
        if guild is None:
            return
        await remove_trap_message(guild)  # 카운터 메시지도 같이 지운다
        set_honeypot(guild.id, channel=None, msg_id=None)
        await self._refresh(interaction)
        await interaction.followup.send("🔕 허니팟 감시를 껐어요.", ephemeral=True)


bot.add_view(HoneypotView())


@tree.command(
    name="허니팟",
    description="허니팟 채널 설정 — 지정 채널에 메시지 보내면 자동 제재 (관리자, 본인 화면에만 뜹니다)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def honeypot_panel(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    await interaction.response.send_message(
        embed=honeypot_embed(guild), view=HoneypotView(guild.id), ephemeral=True
    )


@tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ 권한이 없어요 (관리자 전용)."
    elif isinstance(error, app_commands.TransformerError):
        opt = getattr(error, "opt", None)
        log.warning(
            "슬래시 변환오류: option=%s value=%r transformer=%s err=%r",
            getattr(opt, "name", "?"),
            getattr(error, "value", "?"),
            type(getattr(error, "transformer", None)).__name__,
            error,
        )
        msg = (
            "❌ 입력값이 디스코드에서 봇까지 전달되지 않았어요.\n"
            "1. 디스코드를 **완전 종료 후 재시작** (캐시 문제 1순위)\n"
            "2. **채널·역할을 비우고** `/인증패널`만 입력 → 현재 채널에 게시되는지 확인\n"
            "3. 그래도 안 되면 해당 채널이 텍스트 채널인지 확인 (음성/포럼/공지 불가)"
        )
    else:
        msg = f"❌ 오류: {error}"
        # 오류 보고가 "어느 서버에서"였는지 알 수 있게 컨텍스트를 넘긴다.
        # (로깅 핸들러는 interaction 을 모르므로, 안 넘기면 서버 구분 없이 뿌려진다)
        global _ERR_CTX_GUILD
        _ERR_CTX_GUILD = interaction.guild_id
        log.exception("slash error: %s", error)
        _ERR_CTX_GUILD = None
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def main():
    errs = config.validate()
    if errs:
        print("❌ " + "\n".join(errs))
        print("→ .env.example을 .env로 복사하고 DISCORD_TOKEN을 채우세요.")
        raise SystemExit(1)
    do_web = os.getenv("DISABLE_WEB", "").lower() not in ("1", "true", "yes")
    do_bot = os.getenv("DISABLE_BOT", "").lower() not in ("1", "true", "yes")
    if not do_web and not do_bot:
        print("❌ DISABLE_WEB과 DISABLE_BOT이 둘 다 켜져 있습니다.")
        raise SystemExit(1)
    if do_web:
        for e in webverify.validate():
            log.warning("웹 인증 미설정: %s", e)
        runner = web.AppRunner(webverify.create_app())
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", config.web_port).start()
        log.info("웹 실행: 포트 %s (/health)", config.web_port)
    if do_bot:
        await bot.start(config.token)
    else:
        log.info("봇 비활성 모드 (웹만 실행)")
        webverify.bot_status_fn = lambda: {"bot": "web-only", "guilds": 0}
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # 패널의 Stop/재시작이 보내는 중지 신호. 스택트레이스 대신 한 줄로 정리.
        print("\n⏹ 중지 신호를 받아 종료했습니다.", flush=True)
