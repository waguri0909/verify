"""티켓(문의) 시스템.

흐름: 패널 버튼(결제·주문 / 일반·파트너) → (사유 입력 모달, 켜져 있을 때만) → 전용 채널 생성
→ [닫기]로 잠금 → [삭제] / [재오픈].

- 1인 1개까지만 열림 (이미 열려 있으면 그 채널로 안내)
- 채널명·본문에 티켓 번호 표기 (`ticket-001-유저`, `🎫 티켓 #001`)
- 설정/기록은 `tickets.json` 에 저장되어 재시작해도 유지

등록: bot.py 가 views 를 `bot.add_view` 로 등록하고 슬래시명령을 붙입니다.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import discord

from colors import COLOR_NAMES, DEFAULT_TICKET_COLOR, HEX_HINT, parse_color

log = logging.getLogger("ticket")

BASE_DIR = Path(__file__).parent
STORE = BASE_DIR / "tickets.json"

# 버튼 custom_id (고정값이어야 재시작 후에도 persistent view 로 동작)
OPEN_PURCHASE_BTN = "ticket:open:purchase"
OPEN_GENERAL_BTN = "ticket:open:general"
OPEN_BTN = "ticket:open"  # 예전 단일 버튼 패널 (LegacyPanelView) 에서만 사용
CLOSE_BTN = "ticket:close"
DELETE_BTN = "ticket:delete"
REOPEN_BTN = "ticket:reopen"

# 패널 버튼 2개에 붙는 문의 유형 (티켓 기록에 저장되고 임베드에 표시)
PURCHASE_KIND = "결제 · 주문 문의"
GENERAL_KIND = "일반 · 파트너 문의"

DEFAULT_TITLE = "🎫 고객 지원 센터"
DEFAULT_DESC = (
    "문의가 있으시면 아래 버튼을 눌러주세요.\n"
    "누르면 **나만 보이는 전용 채널**이 생성되고,\n"
    "담당 스태프가 확인한 뒤 바로 답변드릴게요.\n\n"
    "──────────────\n"
    "🔒 한 명당 **1개**까지 열 수 있어요"
)


# ---------- 저장소 (tickets.json) ----------
def _blank() -> dict:
    return {"guilds": {}, "panels": {}, "tickets": {}}


def _load() -> dict:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return _blank()
    if not isinstance(data, dict):
        return _blank()
    for key in ("guilds", "panels", "tickets"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def _save(data: dict) -> None:
    STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _settings_of(data: dict, guild_id: int) -> dict:
    g = data["guilds"].get(str(guild_id))
    if not isinstance(g, dict):
        g = {}
        data["guilds"][str(guild_id)] = g
    g.setdefault("staff", None)      # 스태프 역할 ID
    g.setdefault("category", None)   # 티켓 생성 카테고리 ID
    g.setdefault("reason", True)     # 사유 입력 모달 on/off
    g.setdefault("counter", 0)       # 마지막 발급 번호
    g.setdefault("color", None)      # 패널 왼쪽 바 색상 (None = 기본 인디고)
    g.setdefault("buttons", "both")  # 패널 버튼: both / purchase / general
    return g


def guild_settings(guild_id: int) -> dict:
    data = _load()
    created = str(guild_id) not in data["guilds"]
    g = _settings_of(data, guild_id)
    if created:
        _save(data)
    return g


def update_settings(guild_id: int, **fields) -> dict:
    data = _load()
    g = _settings_of(data, guild_id)
    for k, v in fields.items():
        g[k] = v
    _save(data)
    return g


# ---------- 패널 색상 / 노출 버튼 ----------
DEFAULT_COLOR = DEFAULT_TICKET_COLOR  # 인디고

BUTTON_LABELS = {
    "both": "둘 다",
    "purchase": "💳 결제 문의만",
    "general": "💬 일반·파트너 문의만",
}


def get_color(guild_id: int) -> int:
    """패널 왼쪽 바 색상. 아직 안 정했으면 기본 인디고."""
    raw = guild_settings(guild_id).get("color")
    if raw is None:
        return DEFAULT_COLOR
    try:
        return int(raw) & 0xFFFFFF
    except (TypeError, ValueError):
        return DEFAULT_COLOR


def set_color(guild_id: int, value: int | None) -> dict:
    return update_settings(
        guild_id, color=None if value is None else int(value) & 0xFFFFFF
    )


def button_mode(guild_id: int) -> str:
    mode = guild_settings(guild_id).get("buttons")
    return mode if mode in BUTTON_LABELS else "both"


def set_button_mode(guild_id: int, mode: str) -> dict:
    return update_settings(guild_id, buttons=mode if mode in BUTTON_LABELS else "both")


def button_label(mode: str) -> str:
    return BUTTON_LABELS.get(mode, BUTTON_LABELS["both"])


def save_panel(message_id: int, guild_id: int, channel_id: int | None = None) -> None:
    data = _load()
    rec = data["panels"].get(str(message_id))
    rec = rec if isinstance(rec, dict) else {}
    rec["guild"] = guild_id
    if channel_id is not None:
        rec["channel"] = channel_id
    data["panels"][str(message_id)] = rec
    _save(data)


def panel_count(guild_id: int) -> int:
    return sum(
        1 for v in _load()["panels"].values()
        if isinstance(v, dict) and v.get("guild") == guild_id
    )


def next_number(guild_id: int) -> int:
    data = _load()
    g = _settings_of(data, guild_id)
    g["counter"] = int(g.get("counter", 0)) + 1
    _save(data)
    return g["counter"]


def get_ticket(channel_id: int) -> dict | None:
    rec = _load()["tickets"].get(str(channel_id))
    if isinstance(rec, dict):
        out = dict(rec)
        out["id"] = channel_id
        return out
    return None


def set_ticket(channel_id: int, **fields) -> dict:
    data = _load()
    rec = data["tickets"].get(str(channel_id))
    rec = rec if isinstance(rec, dict) else {}
    rec.update(fields)
    data["tickets"][str(channel_id)] = rec
    _save(data)
    out = dict(rec)
    out["id"] = channel_id
    return out


def remove_ticket(channel_id: int) -> None:
    data = _load()
    if data["tickets"].pop(str(channel_id), None) is not None:
        _save(data)


def find_open(guild_id: int, user_id: int) -> dict | None:
    """유저가 지금 열어 둔 티켓 (채널이 이미 지워졌으면 정리 후 None)."""
    data = _load()
    guild = _bot_guild(guild_id)
    stale = []
    found = None
    for cid, rec in data["tickets"].items():
        if not isinstance(rec, dict) or rec.get("guild") != guild_id:
            continue
        if rec.get("status", "open") != "open":
            continue
        cid_int = _as_int(cid)
        if cid_int is None or (guild is not None and guild.get_channel(cid_int) is None):
            stale.append(cid)
            continue
        if rec.get("owner") == user_id:
            found = dict(rec)
            found["id"] = cid_int
    if stale:
        for cid in stale:
            data["tickets"].pop(cid, None)
        _save(data)
    return found


# 봇 인스턴스는 bot.py 가 여기에 주입 (순환 import 방지)
_BOT = None
_guild_fn = None


def bind(bot=None, guild_fn=None) -> None:
    global _BOT, _guild_fn
    if bot is not None:
        _BOT = bot
    if guild_fn is not None:
        _guild_fn = guild_fn


def _bot_guild(guild_id: int):
    if _guild_fn:
        return _guild_fn(guild_id)
    return _BOT.get_guild(guild_id) if _BOT else None


def prune_missing(guild: discord.Guild) -> int:
    """이 서버에서 채널이 이미 지워진 티켓 기록만 정리 (다른 서버 기록은 건드리지 않음)."""
    data = _load()
    dead = [
        cid
        for cid, rec in data["tickets"].items()
        if isinstance(rec, dict)
        and rec.get("guild") == guild.id
        and _as_int(cid) is not None
        and guild.get_channel(_as_int(cid)) is None
    ]
    for cid in dead:
        data["tickets"].pop(cid, None)
    if dead:
        _save(data)
    return len(dead)


# ---------- 게시된 티켓 패널 갱신 (색상·버튼 조합 변경 시) ----------
async def _locate_panel(guild: discord.Guild, message_id: int, channel_id):
    """기록된 채널부터 찾고, 없으면 서버 텍스트 채널을 하나씩 훑는다."""
    ch = guild.get_channel(_as_int(channel_id)) if channel_id else None
    if isinstance(ch, discord.TextChannel):
        try:
            return await ch.fetch_message(message_id)
        except discord.HTTPException:
            pass
    for ch in guild.text_channels:
        try:
            return await ch.fetch_message(message_id)
        except discord.HTTPException:
            continue
    return None


def _restyle(msg: discord.Message, color: int) -> discord.Embed:
    """게시된 메시지 문구는 그대로 두고 색만 바꾼다 (오래된 footer/시각도 지운다)."""
    if not msg.embeds:
        return build_panel(color=color)
    embed = msg.embeds[0]
    embed.color = discord.Color(color)
    embed.remove_footer()
    embed.timestamp = None
    return embed


# ---------- 패널 자동 발견 (파일 초기화·호스팅 이관에도 목록을 되찾는다) ----------
def _open_panel_button(msg: discord.Message) -> bool:
    """현행 티켓 패널(열기 버튼 1~2개)이면 True.

    구(舊) 1버튼 패널(OPEN_BTN)은 일부러 제외한다 — 등록되면 갱신 때 현행
    2버튼 뷰로 덮어써져 버튼 구성이 바뀌기 때문이다.
    """
    if not msg.embeds:
        return False
    wanted = (OPEN_PURCHASE_BTN, OPEN_GENERAL_BTN)
    for row in msg.components:
        for item in getattr(row, "components", ()):
            if getattr(item, "custom_id", None) in wanted:
                return True
    return False


def _panel_gone(guild: discord.Guild, hint_channel) -> bool:
    """기록된 채널이 그대로 있고 봇도 읽을 수 있는데 메시지를 못 찾았으면 삭제된 것."""
    try:
        ch = guild.get_channel(_as_int(hint_channel)) if hint_channel else None
    except (TypeError, ValueError):
        return False
    if not isinstance(ch, discord.TextChannel):
        return False
    me = guild.me
    if me is None:
        return False
    perms = ch.permissions_for(me)
    return bool(perms.view_channel and perms.read_message_history)


async def discover_panels(guild: discord.Guild) -> int:
    """채널에 살아있는 현행 티켓 패널을 찾아 tickets.json 에 되찾는다. 새로 등록된 개수."""
    me = guild.me
    author_id = me.id if me else 0
    if not author_id:
        return 0
    data = _load()
    known = data["panels"]
    added = 0
    for ch in guild.text_channels[:40]:
        if me is not None and not ch.permissions_for(me).read_message_history:
            continue
        try:
            messages = [m async for m in ch.history(limit=50)]
        except discord.HTTPException:
            continue
        for msg in messages:
            if msg.author.id != author_id or str(msg.id) in known:
                continue
            if not _open_panel_button(msg):
                continue
            known[str(msg.id)] = {"guild": guild.id, "channel": msg.channel.id}
            added += 1
    if added:
        _save(data)
        log.info("티켓 패널 자동 발견 %s개 guild=%s", added, guild.id)
    return added


async def refresh_panels(guild: discord.Guild) -> tuple[int, int, int]:
    """이 서버에 게시된 티켓 패널 전부를 지금 설정(색상·노출 버튼)으로 다시 쓴다."""
    mode = button_mode(guild.id)
    color = get_color(guild.id)
    view = PanelView(mode)
    data = _load()
    ok = fail = missing = 0
    changed = False
    for mid_s, rec in list(data["panels"].items()):
        if not isinstance(rec, dict) or rec.get("guild") != guild.id:
            continue
        mid = _as_int(mid_s)
        if mid is None:
            continue
        msg = await _locate_panel(guild, mid, rec.get("channel"))
        if msg is None:
            missing += 1
            # 읽기 권한이 있는데도 못 찾았으면 메시지가 지워진 것 → 목록에서 정리
            if _panel_gone(guild, rec.get("channel")):
                data["panels"].pop(mid_s, None)
                changed = True
            continue
        cid = getattr(getattr(msg, "channel", None), "id", None)
        if rec.get("channel") != cid:
            rec["channel"] = cid  # 채널 위치 백필 → 다음엔 바로 찾는다
            changed = True
        try:
            await msg.edit(embed=_restyle(msg, color), view=view)
        except discord.HTTPException:
            fail += 1
            continue
        ok += 1
    if changed:
        _save(data)
    return ok, fail, missing


async def announce_panel_refresh(
    interaction: discord.Interaction, guild: discord.Guild
) -> None:
    """게시된 티켓 패널 몇 개를 바꿨는지 사용자에게 알려준다."""
    try:
        ok, fail, missing = await refresh_panels(guild)
    except Exception:
        log.exception("티켓 패널 자동 갱신 실패 guild=%s", guild.id)
        return
    bits = [
        f"🔄 게시된 티켓 패널 **{ok}개**를 "
        f"버튼 {button_label(button_mode(guild.id))} / 색상 "
        f"`#{get_color(guild.id):06X}` (으)로 바꿨어요."
    ]
    if fail:
        bits.append(f"수정 실패 {fail}개 (봇 권한 확인)")
    if missing:
        bits.append(f"찾을 수 없음 {missing}개 (삭제된 패널)")
    try:
        await interaction.followup.send(" ".join(bits), ephemeral=True)
    except discord.HTTPException:
        pass


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------- 표시용 헬퍼 ----------
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _slug(text: str) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^0-9a-z가-힣\-_]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-_")
    return (s or "user")[:40]


def number_label(n: int) -> str:
    return f"#{n:03d}"


def settings_summary(guild_id: int) -> str:
    s = guild_settings(guild_id)
    staff = f"<@{s['staff']}>" if s.get("staff") else "미지정 (봇 권한으로 판단)"
    cat = f"<#{s['category']}>" if s.get("category") else "미지정 (최상위 카테고리)"
    reason = "켜짐" if s.get("reason", True) else "꺼짐"
    return (
        f"• 스태프 역할: {staff}\n"
        f"• 티켓 카테고리: {cat}\n"
        f"• 사유 입력창: **{reason}**\n"
        f"• 패널 버튼: **{button_label(button_mode(guild_id))}**\n"
        f"• 패널 색상: `#{get_color(guild_id):06X}`\n"
        f"• 발급된 티켓: **{int(s.get('counter', 0))}개**\n"
        f"• 게시된 패널: **{panel_count(guild_id)}개**"
    )


def build_panel(
    title: str | None = None,
    desc: str | None = None,
    guild_id: int | None = None,
) -> discord.Embed:
    color = get_color(guild_id) if guild_id is not None else DEFAULT_COLOR
    return discord.Embed(
        title=(title or DEFAULT_TITLE)[:200],
        description=(desc or DEFAULT_DESC)[:2000],
        color=discord.Color(color),
    )


# ---------- 티켓 관리 (패널의 [⚙️ 티켓 관리] 버튼 → ephemeral 조작판) ----------
def can_admin(interaction: discord.Interaction) -> bool:
    """패널 관리 버튼 권한 — 슬래시명령 era 와 동일하게 서버관리 기준."""
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    return bool(user.guild_permissions.manage_guild)


def admin_embed(guild_id: int) -> discord.Embed:
    s = guild_settings(guild_id)
    staff = f"<@&{s['staff']}>" if s.get("staff") else "미지정 (봇 권한으로 판단)"
    cat = f"<#{s['category']}>" if s.get("category") else "미지정 (최상위 카테고리)"
    reason = "🟢 켜짐" if s.get("reason", True) else "🔴 꺼짐"
    embed = discord.Embed(
        title="⚙️ 티켓 관리",
        description="아래에서 티켓 설정을 바로바로 바꿀 수 있어요.",
        color=discord.Color.from_rgb(99, 102, 241),
    )
    embed.add_field(name="스태프 역할", value=staff, inline=True)
    embed.add_field(name="티켓 카테고리", value=cat, inline=True)
    embed.add_field(name="사유 입력창", value=reason, inline=True)
    embed.add_field(
        name="패널 버튼", value=f"**{button_label(button_mode(guild_id))}**", inline=True
    )
    embed.add_field(
        name="패널 색상", value=f"**#{get_color(guild_id):06X}**", inline=True
    )
    embed.add_field(name="발급된 티켓", value=f"{int(s.get('counter', 0))}개", inline=True)
    embed.add_field(name="게시된 패널", value=f"{panel_count(guild_id)}개", inline=True)
    return embed


class TicketAdminView(discord.ui.View):
    """티켓 설정 조작판. ephemeral 메시지에 뜨고 바꿀 때마다 즉시 다시 그려진다."""

    def __init__(self):
        super().__init__(timeout=None)

        role_sel = discord.ui.RoleSelect(
            custom_id="ticket:admin:role",
            placeholder="티켓 스태프 역할 지정",
            min_values=1,
            max_values=1,
            row=0,
        )
        role_sel.callback = self._on_role
        self.add_item(role_sel)

        cat_sel = discord.ui.ChannelSelect(
            custom_id="ticket:admin:category",
            channel_types=[discord.ChannelType.category],
            placeholder="티켓이 생성될 카테고리 지정",
            min_values=1,
            max_values=1,
            row=1,
        )
        cat_sel.callback = self._on_category
        self.add_item(cat_sel)

        for cid, label, style, cb in (
            ("ticket:admin:reason_on", "사유 입력창 켜기",
             discord.ButtonStyle.success, self._on_reason_on),
            ("ticket:admin:reason_off", "사유 입력창 끄기",
             discord.ButtonStyle.danger, self._on_reason_off),
            ("ticket:admin:clear", "역할·카테고리 해제",
             discord.ButtonStyle.secondary, self._on_clear),
            ("ticket:admin:reset", "번호 초기화",
             discord.ButtonStyle.secondary, self._on_reset),
        ):
            b = discord.ui.Button(custom_id=cid, label=label, style=style, row=2)
            b.callback = cb
            self.add_item(b)

        # 3행: 유저 패널에 보여줄 버튼 조합
        for cid, label, style, cb in (
            ("ticket:admin:btn_both", "👋 둘 다",
             discord.ButtonStyle.secondary, self._on_btn_both),
            ("ticket:admin:btn_purchase", "💳 결제 문의만",
             discord.ButtonStyle.blurple, self._on_btn_purchase),
            ("ticket:admin:btn_general", "💬 일반·파트너만",
             discord.ButtonStyle.secondary, self._on_btn_general),
        ):
            b = discord.ui.Button(custom_id=cid, label=label, style=style, row=3)
            b.callback = cb
            self.add_item(b)

        # 4행: 유저 패널 왼쪽 바 색상
        for cid, label, style, cb in (
            ("ticket:admin:color_open", "🎨 색상 설정",
             discord.ButtonStyle.secondary, self._on_color_open),
            ("ticket:admin:color_default", "🟣 기본",
             discord.ButtonStyle.secondary, self._on_color_default),
            ("ticket:admin:color_blue", "🔵 블루",
             discord.ButtonStyle.primary, self._on_color_blue),
            ("ticket:admin:color_green", "🟢 그린",
             discord.ButtonStyle.success, self._on_color_green),
            ("ticket:admin:color_red", "🔴 레드",
             discord.ButtonStyle.danger, self._on_color_red),
        ):
            b = discord.ui.Button(custom_id=cid, label=label, style=style, row=4)
            b.callback = cb
            self.add_item(b)

    # --- 콜백 ---
    # discord.py 는 item.callback(interaction) 만 넘긴다.
    # 데코레이터로 정의하면 _ItemCallback 이 아이템을 따로 끼워주지만,
    # 이렇게 직접 할당하면 interaction 하나만 받는다 → 값은 interaction.data 에서 읽는다.
    @staticmethod
    def _selected(interaction: discord.Interaction) -> int | None:
        values = (interaction.data or {}).get("values") or []
        return int(values[0]) if values else None

    async def _on_role(self, interaction: discord.Interaction) -> None:
        picked = self._selected(interaction)
        if picked is None:
            return
        await self._apply(interaction, staff=picked)

    async def _on_category(self, interaction: discord.Interaction) -> None:
        picked = self._selected(interaction)
        if picked is None:
            return
        await self._apply(interaction, category=picked)

    async def _on_reason_on(self, interaction: discord.Interaction) -> None:
        await self._apply(interaction, reason=True)

    async def _on_reason_off(self, interaction: discord.Interaction) -> None:
        await self._apply(interaction, reason=False)

    async def _on_clear(self, interaction: discord.Interaction) -> None:
        await self._apply(interaction, staff=None, category=None)

    async def _on_reset(self, interaction: discord.Interaction) -> None:
        await self._apply(interaction, counter=0)

    # --- 유저 패널에 보여줄 버튼 조합 ---
    async def _on_btn_both(self, interaction: discord.Interaction) -> None:
        await self._apply(interaction, refresh=True, buttons="both")

    async def _on_btn_purchase(self, interaction: discord.Interaction) -> None:
        await self._apply(interaction, refresh=True, buttons="purchase")

    async def _on_btn_general(self, interaction: discord.Interaction) -> None:
        await self._apply(interaction, refresh=True, buttons="general")

    # --- 유저 패널 왼쪽 바 색상 ---
    async def _on_color_open(self, interaction: discord.Interaction) -> None:
        if not can_admin(interaction):
            await self._deny(interaction)
            return
        await interaction.response.send_modal(TicketColorModal())

    async def _on_color_default(self, interaction: discord.Interaction) -> None:
        await self._apply(interaction, refresh=True, color=None)

    async def _on_color_blue(self, interaction: discord.Interaction) -> None:
        await self._apply(interaction, refresh=True, color=COLOR_NAMES["blue"])

    async def _on_color_green(self, interaction: discord.Interaction) -> None:
        await self._apply(interaction, refresh=True, color=COLOR_NAMES["green"])

    async def _on_color_red(self, interaction: discord.Interaction) -> None:
        await self._apply(interaction, refresh=True, color=COLOR_NAMES["red"])

    # --- 공통 ---
    async def _deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "❌ 권한이 없어요 (서버 관리자 전용).", ephemeral=True
        )

    async def _apply(
        self, interaction: discord.Interaction, *, refresh: bool = False, **fields
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not can_admin(interaction):
            await self._deny(interaction)
            return
        update_settings(guild.id, **fields)
        prune_missing(guild)
        # 응답부터 끝내고(3초 규칙) 패널 갱신은 뒤에서 천천히 돌린다.
        await interaction.response.edit_message(
            embed=admin_embed(guild.id), view=TicketAdminView()
        )
        if refresh:
            await announce_panel_refresh(interaction, guild)


# ---------- 색상 입력 모달 ----------
class TicketColorModal(discord.ui.Modal, title="티켓 패널 색상"):
    color = discord.ui.TextInput(
        label="색상 (HEX 또는 색 이름)",
        placeholder="예: #6366F1 · 인디고 · 블루 · 1ABC9C · 기본",
        required=True,
        max_length=32,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not can_admin(interaction):
            await interaction.response.send_message(
                "❌ 권한이 없어요 (서버 관리자 전용).", ephemeral=True
            )
            return
        raw = str(self.color.value).strip()
        if raw in ("기본", "없음", "초기화", "default", "none", "reset"):
            value = None
        else:
            value = parse_color(raw)
            if value is None:
                await interaction.response.send_message(
                    f"❌ 색상을 못 읽어요. {HEX_HINT}로 적어주세요.",
                    ephemeral=True,
                )
                return
        set_color(guild.id, value)
        await interaction.response.edit_message(
            embed=admin_embed(guild.id), view=TicketAdminView()
        )
        await announce_panel_refresh(interaction, guild)


def build_ticket_embed(rec: dict, status: str, closed_by: int | None = None,
                       note: str | None = None) -> discord.Embed:
    n = int(rec.get("number", 0))
    opened = rec.get("opened") or _now()
    if status == "open":
        title = f"🎫 티켓 {number_label(n)}"
        color = discord.Color.blurple()
        desc = "이 채널은 **문의자와 스태프**만 볼 수 있습니다.\n아래에서 편하게 이야기해주세요."
    else:
        title = f"🔒 티켓 {number_label(n)} · 닫힘"
        color = discord.Color.dark_grey()
        desc = "문의가 종료됐습니다.\n필요하면 **재오픈**하거나 채널을 삭제할 수 있어요."
    embed = discord.Embed(title=title, description=desc, color=color)
    embed.add_field(name="문의자", value=f"<@{rec.get('owner')}> (`{rec.get('owner')}`)", inline=False)
    embed.add_field(name="사유", value=(rec.get("reason") or "미입력")[:1000], inline=False)
    if rec.get("kind"):
        embed.add_field(name="문의 유형", value=str(rec["kind"])[:100], inline=True)
    embed.add_field(name="열린 시각", value=opened, inline=True)
    if status != "open":
        embed.add_field(name="닫은 사람", value=f"<@{closed_by}>" if closed_by else "-", inline=True)
        if note:
            embed.add_field(name="닫은 사유", value=note[:1000], inline=False)
    return embed


# ---------- 권한 ----------
def is_staff(interaction: discord.Interaction) -> bool:
    user = interaction.user
    guild = interaction.guild
    if not isinstance(user, discord.Member) or guild is None:
        return False
    sid = guild_settings(guild.id).get("staff")
    if sid and user.get_role(int(sid)) is not None:
        return True
    p = user.guild_permissions
    return bool(p.administrator or p.manage_channels or p.manage_guild)


def can_manage(interaction: discord.Interaction, rec: dict) -> bool:
    """티켓 닫기/삭제/재오픈 — 문의자 본인 또는 스태프."""
    user = interaction.user
    if isinstance(user, discord.Member) and user.id == _as_int(rec.get("owner")):
        return True
    return is_staff(interaction)


async def _owner_member(guild, rec: dict) -> discord.Member | None:
    """문의자 Member. 캐시에 없으면 한 번 더 조회 (실패해도 None)."""
    owner_id = _as_int(rec.get("owner"))
    if guild is None or owner_id is None:
        return None
    member = guild.get_member(owner_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(owner_id)
    except discord.HTTPException:
        return None


# ---------- 뷰 (재시작 후에도 유지되려면 custom_id 고정 + bot.add_view 필수) ----------
class PanelView(discord.ui.View):
    """유저용 티켓 패널 버튼.

    mode: "both" = 둘 다 / "purchase" = 결제·주문 문의만 /
          "general" = 일반·파트너 문의만
    → 버튼을 골라 담은 뒤 게시하면 그 구조 그대로 패널에 붙는다.
    """

    def __init__(self, mode: str = "both"):
        super().__init__(timeout=None)
        if mode == "purchase":
            hide = {OPEN_GENERAL_BTN}
        elif mode == "general":
            hide = {OPEN_PURCHASE_BTN}
        else:
            hide = set()
        for item in list(self.children):
            if getattr(item, "custom_id", None) in hide:
                self.remove_item(item)

    @discord.ui.button(
        custom_id=OPEN_PURCHASE_BTN, label="💳 결제 · 주문 문의",
        style=discord.ButtonStyle.blurple,
    )
    async def open_purchase(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_open(interaction, PURCHASE_KIND)

    @discord.ui.button(
        custom_id=OPEN_GENERAL_BTN,
        label="💬 일반 · 파트너 문의",
        style=discord.ButtonStyle.secondary,
    )
    async def open_general(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_open(interaction, GENERAL_KIND)

    # 관리 버튼은 유저 패널에 붙이지 않는다 → /티켓패널 이 별도 메시지로 게시


class LegacyPanelView(discord.ui.View):
    """이전에 게시된 단일 버튼 패널이 그대로 눌리도록 유지하는 뷰."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(custom_id=OPEN_BTN, label="티켓 열기 🎫", style=discord.ButtonStyle.blurple)
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_open(interaction, GENERAL_KIND)


class TicketView(discord.ui.View):
    """열려 있는 티켓 채널의 컨트롤."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(custom_id=CLOSE_BTN, label="닫기 🔒", style=discord.ButtonStyle.red)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await close_ticket(interaction)


class ClosedTicketView(discord.ui.View):
    """닫힌 티켓 채널의 컨트롤."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(custom_id=DELETE_BTN, label="삭제 🗑️", style=discord.ButtonStyle.red)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await delete_ticket(interaction)

    @discord.ui.button(custom_id=REOPEN_BTN, label="재오픈 ↩️", style=discord.ButtonStyle.green)
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reopen_ticket(interaction)


class ReasonModal(discord.ui.Modal, title="티켓 열기"):
    reason = discord.ui.TextInput(
        label="문의 사유",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False,
        placeholder="무엇을 도와드릴까요? (비워두고 열 수도 있어요)",
    )

    def __init__(self, kind: str | None = None):
        super().__init__()
        self.kind = kind

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.reason.value or "").strip()
        await _create(interaction, text or None, self.kind)


# ---------- 티켓 열기 ----------
async def handle_open(interaction: discord.Interaction, kind: str | None = None) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
        return

    existing = find_open(guild.id, interaction.user.id)
    if existing:
        await interaction.response.send_message(
            f"⚠️ 이미 열려 있는 티켓이 있어요: <#{existing['id']}>\n"
            f"먼저 그 채널에서 이야기를 이어가거나, 닫은 뒤 다시 시도해주세요.",
            ephemeral=True,
        )
        return

    if guild_settings(guild.id).get("reason", True):
        await interaction.response.send_modal(ReasonModal(kind))
    else:
        await _create(interaction, None, kind)


async def _create(
    interaction: discord.Interaction, reason: str | None, kind: str | None = None
) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
        return

    user = interaction.user
    prune_missing(guild)
    if find_open(guild.id, user.id):
        await interaction.response.send_message(
            "⚠️ 이미 열려 있는 티켓이 있어요. 잠시 후 다시 확인해주세요.", ephemeral=True
        )
        return

    settings = guild_settings(guild.id)
    number = next_number(guild.id)
    display = getattr(user, "display_name", None) or getattr(user, "name", str(user))
    name = f"ticket-{number:03d}-{_slug(display)}"[:100]

    overwrites: dict = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True,
            manage_messages=True, read_message_history=True,
        ),
    }
    if isinstance(user, discord.Member):
        overwrites[user] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True,
            embed_links=True, read_message_history=True,
        )
    staff_id = settings.get("staff")
    staff_role = guild.get_role(int(staff_id)) if staff_id else None
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True,
            embed_links=True, read_message_history=True,
        )

    category = None
    if settings.get("category"):
        cat = guild.get_channel(int(settings["category"]))
        if isinstance(cat, discord.CategoryChannel):
            category = cat

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        channel = await guild.create_text_channel(
            name,
            overwrites=overwrites,
            category=category,
            reason=f"티켓 {number_label(number)} 개설 - {user}",
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ 티켓 채널을 만들 권한이 없어요. 서버 설정 → **채널 관리** 권한을 확인해주세요.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ 채널 생성 실패: {e}", ephemeral=True)
        return

    rec = set_ticket(
        channel.id,
        guild=guild.id,
        owner=user.id,
        number=number,
        reason=reason,
        kind=kind,
        opened=_now(),
        status="open",
        closed_by=None,
        note=None,
    )
    embed = build_ticket_embed(rec, "open")
    try:
        msg = await channel.send(
            f"{user.mention}님 **문의가 접수됐습니다.** "
            f"담당 스태프가 확인하면 곧 답변드릴게요.",
            embed=embed,
            view=TicketView(),
        )
        set_ticket(channel.id, msg=msg.id)
    except discord.HTTPException:
        log.warning("티켓 채널 첫 메시지 전송 실패: %s", channel.id)

    await interaction.followup.send(
        f"✅ 티켓 {number_label(number)} 열렸어요 → {channel.mention}", ephemeral=True
    )


# ---------- 상태 변경 공통 ----------
async def _refresh_message(channel, rec: dict, status: str,
                           closed_by: int | None = None, note: str | None = None) -> None:
    embed = build_ticket_embed(rec, status, closed_by, note)
    view = TicketView() if status == "open" else ClosedTicketView()
    msg = None
    if rec.get("msg"):
        try:
            msg = await channel.fetch_message(int(rec["msg"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
            msg = None
    if msg is not None:
        try:
            await msg.edit(embed=embed, view=view)
            return
        except discord.HTTPException:
            pass
    sent = await channel.send(embed=embed, view=view)
    set_ticket(channel.id, msg=sent.id)


async def close_ticket(interaction: discord.Interaction, note: str | None = None) -> None:
    channel = interaction.channel
    guild = interaction.guild
    rec = get_ticket(channel.id) if channel is not None else None
    if rec is None or rec.get("status", "open") != "open":
        await interaction.response.send_message("❌ 여기는 열려 있는 티켓 채널이 아니에요.", ephemeral=True)
        return
    if not can_manage(interaction, rec):
        await interaction.response.send_message(
            "❌ 티켓을 닫으려면 스태프 권한이 필요해요.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    over = dict(channel.overwrites)
    member = await _owner_member(guild, rec)
    if member is not None:
        po = over.get(member) or discord.PermissionOverwrite()
        po.update(view_channel=True, send_messages=False, read_message_history=True)
        over[member] = po

    suffix = channel.name
    if suffix.startswith("ticket-"):
        suffix = suffix[len("ticket-"):]
    new_name = f"closed-{suffix}"[:100]

    try:
        await channel.edit(overwrites=over, name=new_name, reason="티켓 닫힘")
    except discord.Forbidden:
        await interaction.followup.send("❌ 채널을 잠글 권한이 없어요 (봇에 **채널 관리** 필요).", ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ 티켓 잠금 실패: {e}", ephemeral=True)
        return

    set_ticket(channel.id, status="closed", closed_by=interaction.user.id, note=note)
    rec = get_ticket(channel.id)
    try:
        await _refresh_message(channel, rec, "closed", interaction.user.id, note)
    except discord.HTTPException:
        log.warning("닫힘 상태 메시지 갱신 실패: %s", channel.id)

    await interaction.followup.send(
        f"🔒 티켓 {number_label(int(rec.get('number', 0)))}을 닫았어요. "
        f"재열기는 [재오픈], 정리는 [삭제] 버튼으로.",
        ephemeral=True,
    )


async def reopen_ticket(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    guild = interaction.guild
    rec = get_ticket(channel.id) if channel is not None else None
    if rec is None or rec.get("status") == "open":
        await interaction.response.send_message("❌ 닫혀 있는 티켓만 재오픈할 수 있어요.", ephemeral=True)
        return
    if not can_manage(interaction, rec):
        await interaction.response.send_message("❌ 재오픈은 스태프 권한이 필요해요.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    over = dict(channel.overwrites)
    member = await _owner_member(guild, rec)
    if member is not None:
        po = over.get(member) or discord.PermissionOverwrite()
        po.update(view_channel=True, send_messages=True, read_message_history=True)
        over[member] = po

    name = channel.name
    if name.startswith("closed-"):
        name = "ticket-" + name[len("closed-"):]
    try:
        await channel.edit(overwrites=over, name=name[:100], reason="티켓 재오픈")
    except discord.Forbidden:
        await interaction.followup.send("❌ 채널을 고칠 권한이 없어요.", ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ 재오픈 실패: {e}", ephemeral=True)
        return

    set_ticket(channel.id, status="open", closed_by=None, note=None)
    rec = get_ticket(channel.id)
    try:
        await _refresh_message(channel, rec, "open")
    except discord.HTTPException:
        log.warning("재오픈 메시지 갱신 실패: %s", channel.id)

    await interaction.followup.send(
        f"↩️ 티켓 {number_label(int(rec.get('number', 0)))}을 다시 열었어요.", ephemeral=True
    )


async def delete_ticket(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    rec = get_ticket(channel.id) if channel is not None else None
    if rec is None:
        await interaction.response.send_message("❌ 티켓 기록이 없는 채널이에요.", ephemeral=True)
        return
    if not can_manage(interaction, rec):
        await interaction.response.send_message("❌ 삭제는 스태프 권한이 필요해요.", ephemeral=True)
        return

    label = number_label(int(rec.get("number", 0)))
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        await channel.delete(reason=f"티켓 {label} 삭제 - {interaction.user}")
    except discord.Forbidden:
        await interaction.followup.send("❌ 채널을 삭제할 권한이 없어요.", ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ 삭제 실패: {e}", ephemeral=True)
        return
    remove_ticket(channel.id)
    try:
        await interaction.followup.send(f"🗑️ 티켓 {label} 삭제 완료.", ephemeral=True)
    except discord.HTTPException:
        pass
