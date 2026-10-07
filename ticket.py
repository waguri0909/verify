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


def save_panel(message_id: int, guild_id: int) -> None:
    data = _load()
    data["panels"][str(message_id)] = {"guild": guild_id}
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
        f"• 발급된 티켓: **{int(s.get('counter', 0))}개**\n"
        f"• 게시된 패널: **{panel_count(guild_id)}개**"
    )


def build_panel(title: str | None = None, desc: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=(title or DEFAULT_TITLE)[:200],
        description=(desc or DEFAULT_DESC)[:2000],
        color=discord.Color.from_rgb(99, 102, 241),
        timestamp=datetime.now(timezone.utc),
    )
    return embed


def build_ticket_embed(rec: dict, status: str, closed_by: int | None = None,
                       note: str | None = None) -> discord.Embed:
    n = int(rec.get("number", 0))
    opened = rec.get("opened") or _now()
    if status == "open":
        title = f"🎫 티켓 {number_label(n)}"
        color = discord.Color.blurple()
        footer = "대화가 끝나면 [닫기] 를 눌러주세요"
        desc = "이 채널은 **문의자와 스태프**만 볼 수 있습니다.\n아래에서 편하게 이야기해주세요."
    else:
        title = f"🔒 티켓 {number_label(n)} · 닫힘"
        color = discord.Color.dark_grey()
        footer = "다시 열거나 삭제하려면 아래 버튼을 눌러주세요"
        desc = "문의가 종료됐습니다.\n필요하면 **재오픈**하거나 채널을 삭제할 수 있어요."
    embed = discord.Embed(
        title=title, description=desc, color=color, timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="문의자", value=f"<@{rec.get('owner')}> (`{rec.get('owner')}`)", inline=False)
    embed.add_field(name="사유", value=(rec.get("reason") or "미입력")[:1000], inline=False)
    if rec.get("kind"):
        embed.add_field(name="문의 유형", value=str(rec["kind"])[:100], inline=True)
    embed.add_field(name="열린 시각", value=opened, inline=True)
    if status != "open":
        embed.add_field(name="닫은 사람", value=f"<@{closed_by}>" if closed_by else "-", inline=True)
        if note:
            embed.add_field(name="닫은 사유", value=note[:1000], inline=False)
    embed.set_footer(text=footer)
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
    def __init__(self):
        super().__init__(timeout=None)

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
