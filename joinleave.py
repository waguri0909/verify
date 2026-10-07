"""입장/퇴장 로그 + 전용 배너 이미지 생성.

설정 저장: logs.json (서버별 채널 / on-off / 이미지 on-off)

흐름:
  bot.py 의 on_member_join / on_member_remove 가 send() 를 부름
  → 설정된 채널이 있으면 임베드(+배너) 전송, 없으면 False 반환
    → bot.py 는 False 면 기존 텍스트 로그(LOG_CHANNEL_ID)로 대체

배너는 Pillow 로 매번 새로 그립니다. 폰트는 assets/fonts 에 있는
Pretendard OTF 를 씁니다. 이미지 생성이 실패해도 로그는 반드시 나갑니다.
"""

import asyncio
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

log = logging.getLogger("joinleave")

BASE_DIR = Path(__file__).parent
STORE = BASE_DIR / "logs.json"
FONT_DIR = BASE_DIR / "assets" / "fonts"

# 배너 크기 / 레이아웃  (왼쪽 유저 아바타 · 가운데 텍스트 · 오른쪽 서버 아이콘)
W, H = 1120, 360
AVATAR_D = 210
AVATAR_X, AVATAR_Y = 70, 75
TEXT_X = 330
BAR_Y = 84
EYEBROW_Y = 142
TITLE_Y = 224
NAME_Y = 292
ICON_D = 200
ICON_X, ICON_Y = 855, 80
ICON_LABEL_Y = 322

# 색
BG_TOP = (11, 14, 26)        # #0B0E1A
BG_BOTTOM = (24, 19, 56)     # #181338
GLOW_A = (88, 101, 242)      # indigo
GLOW_B = (139, 92, 246)      # violet
TEXT_MAIN = (244, 246, 255)
TEXT_SUB = (176, 183, 214)
TEXT_MUTED = (128, 136, 170)

EYE = {
    "join": ("WELCOME", "환영합니다", (87, 242, 135), discord.Color.from_rgb(59, 165, 93)),
    "leave": ("FAREWELL", "다음에 또 만나요", (255, 107, 107), discord.Color.from_rgb(237, 66, 69)),
}

DEFAULTS = {"channel": None, "enabled": False, "image": True}
_FONT_CACHE: dict = {}
_FALLOFF: Image.Image | None = None


# ---------- 저장소 ----------
def _load() -> dict:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("guilds"), dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"guilds": {}}


def _save(data: dict) -> None:
    STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def settings(guild_id: int) -> dict:
    g = _load()["guilds"].get(str(guild_id))
    out = dict(DEFAULTS)
    if isinstance(g, dict):
        out.update({k: g[k] for k in DEFAULTS if k in g})
    return out


def update(guild_id: int, **fields) -> dict:
    data = _load()
    g = data["guilds"].setdefault(str(guild_id), {})
    for k, v in fields.items():
        if k in DEFAULTS:
            g[k] = v
    _save(data)
    return settings(guild_id)


def summary(guild_id: int) -> str:
    s = settings(guild_id)
    ch = f"<#{s['channel']}>" if s.get("channel") else "미지정"
    on = "켜짐" if s.get("enabled") else "꺼짐"
    img = "붙임" if s.get("image") else "안 붙임"
    return (
        f"• 로그 채널: {ch}\n"
        f"• 입장/퇴장 로그: **{on}**\n"
        f"• 안내 이미지: **{img}**"
    )


# ---------- 폰트 / 헬퍼 ----------
def _font(size: int, bold: bool = True):
    key = (size, bold)
    f = _FONT_CACHE.get(key)
    if f is None:
        path = FONT_DIR / ("Pretendard-Bold.otf" if bold else "Pretendard-Regular.otf")
        try:
            f = ImageFont.truetype(str(path), size)
        except Exception:
            f = _fallback_font(size)
        _FONT_CACHE[key] = f
    return f


def _fallback_font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _fit(draw, text: str, size: int, max_w: int, bold: bool = True):
    """(폰트, 조정된 문자열) — max_w 를 넘으면 크기를 줄이고 그래도 넘으면 잘라냄."""
    text = str(text) or "-"
    for s in range(size, 17, -2):
        f = _font(s, bold)
        if draw.textlength(text, font=f) <= max_w:
            return f, text
    f = _font(18, bold)
    if draw.textlength(text, font=f) > max_w:
        while text and draw.textlength(text + "…", font=f) > max_w:
            text = text[:-1]
        text = text + "…"
    return f, text


def _gradient() -> Image.Image:
    """45° 대각 그라디언트.

    rotate(-45) 방식은 빈 모서리가 검정으로 채워져 대각 이음새를 남긴다.
    가로/세로 그라디언트를 합치면 이음새 없이 매끈하다.
    """
    h = Image.linear_gradient("L").transpose(Image.ROTATE_90).resize((W, H), Image.BILINEAR)
    v = Image.linear_gradient("L").resize((W, H), Image.BILINEAR)
    g = ImageChops.add(h, v, scale=2)  # (가로+세로)/2 → 좌상단 0 → 우하단 255
    return ImageOps.colorize(g, black=BG_TOP, white=BG_BOTTOM).convert("RGB")


def _smoothstep(v: int) -> int:
    """가운데=255, 반지름 끝=0. 억지로 0을 만들지 않으면 사각 테두리에 이음새가 생긴다."""
    f = 1.0 - v / 181.0  # radial_gradient = min(255, 거리*√2) → 181 에서 0
    if f <= 0.0:
        return 0
    if f >= 1.0:
        return 255
    return int(255 * (f * f * (3.0 - 2.0 * f)))


def _falloff() -> Image.Image:
    """가운데 밝고 가장자리 0인 원형 감쇠 마스크 (계산 결과 캐시)."""
    global _FALLOFF
    if _FALLOFF is None:
        _FALLOFF = Image.radial_gradient("L").point(_smoothstep)
    return _FALLOFF


def _glow(img: Image.Image, color, center, radius: int, strength: float) -> Image.Image:
    grad = _falloff().resize((radius * 2, radius * 2), Image.BILINEAR)
    mask = Image.new("L", (W, H), 0)
    mask.paste(grad, (center[0] - radius, center[1] - radius))
    mask = mask.point(lambda v: int(v * strength))
    return Image.composite(Image.new("RGB", (W, H), color), img, mask)


def _paste_avatar(canvas: Image.Image, data: bytes, accent) -> None:
    size = AVATAR_D * 4
    src = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = 10  # 4x 기준 → 실제 2.5px
    ImageDraw.Draw(ring).ellipse((d // 2, d // 2, size - d // 2, size - d // 2),
                                 outline=(*accent, 255), width=d)
    src = Image.alpha_composite(src, ring)
    src = src.resize((AVATAR_D, AVATAR_D), Image.LANCZOS)
    m = mask.resize((AVATAR_D, AVATAR_D), Image.LANCZOS)
    canvas.paste(src, (AVATAR_X, AVATAR_Y), m)


def _paste_icon(canvas: Image.Image, data: bytes | None, label: str, accent) -> None:
    """오른쪽 서버 아이콘(라운드 스퀘어). 아이콘이 없으면 첫 글자 타일로 대체."""
    s = ICON_D * 4
    if data:
        src = Image.open(io.BytesIO(data)).convert("RGBA")
        w, h = src.size
        side = min(w, h)
        src = src.crop(((w - side) // 2, (h - side) // 2,
                        (w + side) // 2, (h + side) // 2)).resize((s, s), Image.LANCZOS)
    else:
        src = Image.new("RGBA", (s, s), (30, 34, 60, 255))
        ch = (label or "?").strip()[:1].upper()
        ImageDraw.Draw(src).text((s / 2, s / 2), ch, font=_font(int(s * 0.44), True),
                                 fill=(*accent, 255), anchor="mm")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s, s), radius=96, fill=255)
    src.putalpha(mask)
    ImageDraw.Draw(src).rounded_rectangle((4, 4, s - 4, s - 4), radius=92,
                                          outline=(255, 255, 255, 28), width=8)
    src = src.resize((ICON_D, ICON_D), Image.LANCZOS)
    canvas.paste(src, (ICON_X, ICON_Y), src)


def build_banner(kind: str, avatar: bytes | None, name: str, guild_name: str,
                 guild_icon: bytes | None = None) -> io.BytesIO:
    """입장/퇴장 전용 배너. 실패해도 예외는 밖으로 던진다(호출부에서 삼킴)."""
    eye, title, accent, _ = EYE[kind]
    img = _gradient()
    img = _glow(img, GLOW_A, (250, 40), 400, 0.50)
    img = _glow(img, GLOW_B, (W - 120, H - 20), 360, 0.42)
    draw = ImageDraw.Draw(img)

    # 왼쪽 유저 아바타 (없으면 빈 원)
    if avatar:
        _paste_avatar(img, avatar, accent)
    else:
        draw.ellipse((AVATAR_X, AVATAR_Y, AVATAR_X + AVATAR_D, AVATAR_Y + AVATAR_D),
                     fill=(28, 32, 56), outline=(*accent, 255), width=6)

    # 오른쪽 서버 아이콘 — 빈 공간을 메우고 "어느 서버인지"까지 보여줌
    _paste_icon(img, guild_icon, guild_name, accent)

    max_w = ICON_X - TEXT_X - 55

    # 강조 바
    draw.rounded_rectangle((TEXT_X, BAR_Y, TEXT_X + 56, BAR_Y + 6), radius=3, fill=accent)

    # eyebrow (자간 넓게)
    f_eye = _font(24, True)
    x = TEXT_X
    for ch in eye:
        draw.text((x, EYEBROW_Y), ch, font=f_eye, fill=accent, anchor="ls")
        x += draw.textlength(ch, font=f_eye) + 7

    f, t = _fit(draw, title, 62, max_w, bold=True)
    draw.text((TEXT_X, TITLE_Y), t, font=f, fill=TEXT_MAIN, anchor="ls")

    f, t = _fit(draw, name, 40, max_w, bold=True)
    draw.text((TEXT_X, NAME_Y), t, font=f, fill=TEXT_SUB, anchor="ls")

    # 서버 이름은 아이콘 아래 가운데 정렬
    f, t = _fit(draw, guild_name, 22, ICON_D + 40, bold=False)
    draw.text((ICON_X + ICON_D // 2, ICON_LABEL_Y), t, font=f,
              fill=TEXT_MUTED, anchor="ms")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ---------- 텍스트/날짜 헬퍼 ----------
KST = timezone(timedelta(hours=9))


def _date(dt: datetime | None) -> str:
    return dt.astimezone(KST).strftime("%Y년 %m월 %d일") if dt else "알 수 없음"


def _duration(delta: timedelta) -> str:
    s = max(int(delta.total_seconds()), 0)
    if s < 60:
        return f"{s}초"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}분"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}시간 {m}분"
    d, h = divmod(h, 24)
    return f"{d}일 {h}시간"


async def _avatar_of(member: discord.Member) -> bytes | None:
    try:
        return await member.display_avatar.with_size(256).read()
    except Exception as e:
        log.warning("아바타 받기 실패: %s", e)
        return None


async def _guild_icon_of(guild: discord.Guild) -> bytes | None:
    try:
        if guild.icon is not None:
            return await guild.icon.with_size(256).read()
    except Exception as e:
        log.warning("서버 아이콘 받기 실패: %s", e)
    return None


# ---------- 로그 전송 ----------
def is_active(guild_id: int) -> bool:
    s = settings(guild_id)
    return bool(s.get("enabled") and s.get("channel"))


async def send(member: discord.Member, kind: str, note: str | None = None) -> bool:
    """설정돼 있으면 로그를 보내고 True, 아니면 False (호출부가 대체 로그를 씀)."""
    guild = member.guild
    if not is_active(guild.id):
        return False
    s = settings(guild.id)
    channel = guild.get_channel(int(s["channel"]))
    if not isinstance(channel, discord.TextChannel):
        return False

    eye, _, _, color = EYE[kind]
    joined = getattr(member, "joined_at", None)
    safe_name = discord.utils.escape_markdown(str(member))[:80]

    if kind == "join":
        title = "🟢 멤버 입장"
        desc = f"**{safe_name}** 님이 서버에 합류했습니다."
    else:
        title = "🔴 멤버 퇴장"
        desc = f"**{safe_name}** 님이 서버를 떠났습니다."

    embed = discord.Embed(title=title, description=desc, color=color,
                          timestamp=datetime.now(timezone.utc))
    embed.add_field(name="서버 가입일", value=_date(joined), inline=True)
    embed.add_field(name="디스코드 가입일", value=_date(member.created_at), inline=True)
    if kind == "join":
        embed.add_field(name="현재 인원", value=f"{guild.member_count}명", inline=True)
    else:
        stayed = _duration(datetime.now(timezone.utc) - joined) if joined else "알 수 없음"
        embed.add_field(name="서버 체류 시간", value=stayed, inline=True)
    if note:
        embed.add_field(name="⚠️ 확인", value=note[:300], inline=False)
    embed.set_footer(text=f"{guild.name} · 멤버 {guild.member_count}명")
    if member.display_name:
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)

    files = []
    if s.get("image"):
        try:
            avatar, icon = await asyncio.gather(_avatar_of(member), _guild_icon_of(guild))
            buf = build_banner(kind, avatar, str(member), guild.name, icon)
            fname = f"{kind}_{member.id}.png"
            embed.set_image(url=f"attachment://{fname}")
            files.append(discord.File(buf, filename=fname))
        except Exception as e:
            log.warning("배너 생성 실패(이미지 없이 전송): %s", e)

    try:
        await channel.send(embed=embed, files=files or discord.utils.MISSING)
        return True
    except discord.Forbidden:
        log.warning("로그 채널 전송 권한 없음: %s", channel.id)
        return False
    except discord.HTTPException as e:
        log.warning("로그 전송 실패: %s", e)
        return False


# ---------- 미리보기 ----------
async def preview(member: discord.Member, channel: discord.abc.Messageable, kind: str = "join"):
    """설정과 무관하게 지금 설정대로 이미지를 미리 그려 보여준다."""
    avatar, icon = await asyncio.gather(
        _avatar_of(member), _guild_icon_of(member.guild)
    )
    buf = build_banner(kind, avatar, str(member), member.guild.name, icon)
    await channel.send(
        content="🖼️ **안내 이미지 미리보기**",
        file=discord.File(buf, filename=f"preview_{kind}.png"),
    )
