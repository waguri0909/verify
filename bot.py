"""Discord 웹 OAuth 인증봇.

흐름: 패널 [웹에서 인증하기] → Discord 승인 → 웹사이트에서
이메일인증여부/중복IP 검사 → REST로 역할 지급.
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from aiohttp import web
from discord import app_commands
from dotenv import load_dotenv

import recover
import webapi
import webverify

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("verify-bot")


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
    guild_id: int | None = _get_int("GUILD_ID")
    web_port: int = int(os.getenv("PORT", "") or os.getenv("WEB_PORT", "8000") or 8000)

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


def get_guild_panel_role(guild_id: int) -> int | None:
    """이 서버에 역할 지정된 패널이 있으면 첫 번째 인증 롤 ID."""
    for v in _load_json(PANEL_FILE).values():
        if isinstance(v, dict) and v.get("guild") == guild_id and v.get("verified"):
            return int(v["verified"])
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
    panel = get_panel_setting(message_id)
    if panel and panel.get("verified"):
        r = guild.get_role(int(panel["verified"]))
        if r:
            return r
    role_id = get_guild_panel_role(guild.id)
    if role_id:
        r = guild.get_role(role_id)
        if r:
            return r
    return resolve_role(guild, config.verified_role)


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
DEFAULT_LINE2 = "아래 **웹에서 인증하기** 버튼을 눌러주세요.\n인증하면 나머지 채널이 보입니다."


def build_panel(
    role: discord.Role | None = None,
    title: str | None = None,
    line1: str | None = None,
    line2: str | None = None,
) -> discord.Embed:
    title = (title or DEFAULT_TITLE)[:200]
    line1 = (line1 or DEFAULT_LINE1)[:1000]
    line2 = (line2 or DEFAULT_LINE2)[:1500]
    role_line = f"\n인증하면 {role.mention} 롤이 지급됩니다." if role else ""
    embed = discord.Embed(
        title=title,
        description=f"{line1}\n\n{line2}{role_line}",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="문의: 서버 관리자")
    return embed


def link_view(url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="웹에서 인증하기 🌐", url=url))
    return view


# ---------- 이벤트 ----------
@bot.event
async def on_ready():
    errors = config.validate() + webverify.validate()
    if errors:
        log.error("설정 오류: %s", errors)
    else:
        log.info("로그인: %s", bot.user)
    try:
        if config.guild_id:
            guild = discord.Object(id=config.guild_id)
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        else:
            await tree.sync()
        log.info("슬래시 명령 동기화 완료")
    except Exception as e:
        log.exception("명령 동기화 실패: %s", e)


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
    unverified = resolve_role(guild, config.unverified_role)

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

    await send_log(guild, f"👋 **입장** {member.mention} (`{member}`){warn}")

    if config.auth_channel_id:
        ch = guild.get_channel(config.auth_channel_id)
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(
                    f"{member.mention}님 환영합니다! 위 **웹에서 인증하기** 버튼을 눌러주세요. 🎉",
                    delete_after=30,
                )
            except discord.HTTPException:
                pass


# ---------- 슬래시 명령 ----------
@tree.command(name="인증패널", description="인증 패널 게시 + 역할/문구 지정 (관리자)")
@app_commands.describe(
    채널="패널 올릴 텍스트 채널 (비우면 현재 채널)",
    역할="인증하면 지급할 역할 (비우면 .env 기본값)",
    제목="패널 제목 (비우면 기본값)",
    문장1="맨 위 문장: 환영 문구 (비우면 기본값)",
    문장2="아래 블록 전체 (버튼 안내+채널 안내, 비우면 기본값)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_panel(
    interaction: discord.Interaction,
    채널: discord.abc.GuildChannel | None = None,
    역할: discord.Role | None = None,
    제목: str | None = None,
    문장1: str | None = None,
    문장2: str | None = None,
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
    try:
        await webapi.api_ping()
    except webapi.ApiError as e:
        await interaction.response.send_message(
            f"❌ 웹 서버에 연결할 수 없습니다: {e}",
            ephemeral=True,
        )
        return
    # 랜딩 페이지를 거쳐 승인 → 스코프가 바뀌어도 기존 패널이 그대로 유효
    base = webverify.env("WEB_PUBLIC_URL").rstrip("/")
    url = f"{base}/?guild={guild.id}&role={verified.id}"
    embed = build_panel(verified, 제목, 문장1, 문장2)
    msg = await target.send(embed=embed, view=link_view(url))
    panels = _load_json(PANEL_FILE)
    panels[str(msg.id)] = {
        "guild": guild.id,
        "verified": verified.id,
        "title": 제목,
        "line1": 문장1,
        "line2": 문장2,
    }
    _save_json(PANEL_FILE, panels)
    desc = f"{target.mention}에 인증 패널을 올렸어요. ✅\n지급 역할: {verified.mention}"
    await interaction.response.send_message(desc, ephemeral=True)


@tree.command(name="복구현황", description="복구키에 쌓인 인원 확인 (관리자)")
@app_commands.describe(키="봇 초대 시 DM으로 받은 복구키")
@app_commands.checks.has_permissions(manage_guild=True)
async def recover_status(interaction: discord.Interaction, 키: str):
    gid = recover.key_to_guild(키)
    if gid is None:
        await interaction.response.send_message("❌ 유효하지 않은 복구키입니다.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        n = await recover.backup_count(gid)
    except webapi.ApiError as e:
        await interaction.followup.send(f"❌ 웹 서버 연결 실패: {e}", ephemeral=True)
        return
    await interaction.followup.send(
        f"🔑 이 복구키에 **{n}명** 쌓여 있습니다.\n(웹 인증 + 참가 승인을 한 사람만 복구 가능)",
        ephemeral=True,
    )


@tree.command(name="복구", description="복구키로 백업된 멤버들을 이 서버에 초대 (관리자)")
@app_commands.describe(키="봇 초대 시 DM으로 받은 복구키")
@app_commands.checks.has_permissions(manage_guild=True)
async def recover_run(interaction: discord.Interaction, 키: str):
    guild = interaction.guild
    assert guild is not None
    gid = recover.key_to_guild(키)
    if gid is None:
        await interaction.response.send_message("❌ 유효하지 않은 복구키입니다.", ephemeral=True)
        return
    me = guild.me
    if me is None or not me.guild_permissions.create_instant_invite:
        await interaction.response.send_message(
            "❌ 봇에 **멤버 초대하기** 권한이 없습니다. 역할 설정을 확인해주세요.", ephemeral=True
        )
        return
    total = 0
    try:
        total = await recover.backup_count(gid)
    except webapi.ApiError as e:
        await interaction.response.send_message(f"❌ 웹 서버 연결 실패: {e}", ephemeral=True)
        return
    if total == 0:
        await interaction.response.send_message("❌ 이 복구키에 쌓인 인원이 없습니다.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await interaction.followup.send(
        f"🔄 복구 시작: {total}명 (예상 약 {total * 1.5 / 60:.0f}분)…", ephemeral=True
    )
    last_edit = 0.0
    import time as _time

    async def progress(done: int, total_n: int):
        nonlocal last_edit
        if done == total_n or done % 10 == 0 or _time.time() - last_edit > 10:
            last_edit = _time.time()
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


@tree.command(name="인증초기화", description="유저 인증 해제 (관리자)")
@app_commands.describe(유저="해제할 유저")
@app_commands.checks.has_permissions(manage_roles=True)
async def unverify(interaction: discord.Interaction, 유저: discord.Member):
    guild = interaction.guild
    assert guild is not None
    verified = resolve_verified_role(guild)
    unverified = resolve_role(guild, config.unverified_role)
    try:
        if verified and verified in 유저.roles:
            await 유저.remove_roles(verified, reason="관리자 인증 초기화")
        if unverified and unverified not in 유저.roles:
            await 유저.add_roles(unverified, reason="관리자 인증 초기화")
    except discord.Forbidden:
        await interaction.response.send_message("❌ 권한 부족 (봇 롤 순서 확인)", ephemeral=True)
        return
    await interaction.response.send_message(f"{유저.mention} 인증을 해제했어요.", ephemeral=True)
    await send_log(guild, f"🔄 **인증해제** {유저.mention} (by {interaction.user.mention})")


@tree.command(name="인증로그", description="인증 성공 시 전송될 웹훅 설정 (관리자)")
@app_commands.describe(웹훅="채널 설정 → 연동 → 웹훅 → 새 웹훅 → URL 복사")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_verify_log(interaction: discord.Interaction, 웹훅: str):
    guild = interaction.guild
    assert guild is not None
    url = 웹훅.strip()
    await interaction.response.defer(ephemeral=True)
    try:
        ok, detail = await webapi.webhook_test(guild.id, url)
    except webapi.ApiError as e:
        await interaction.followup.send(f"❌ 웹 서버 연결 실패: {e}", ephemeral=True)
        return
    if not ok:
        hint = ""
        if "404" in detail:
            hint = "\n💡 404 = 웹훅이 삭제됐거나 URL 오타. 채널 설정 → 연동 → 웹훅에서 URL을 다시 복사해주세요."
        elif "401" in detail or "403" in detail:
            hint = "\n💡 401/403 = 토큰이 유효하지 않음. 웹훅을 새로 만드세요."
        elif "형식" in detail:
            hint = "\n💡 `https://discord.com/api/webhooks/...` 형태여야 해요."
        await interaction.followup.send(
            f"❌ 웹훅 전송 테스트 실패.\n상세: `{detail}`{hint}",
            ephemeral=True,
        )
        return
    await interaction.followup.send("✅ 인증 로그 웹훅이 설정됐어요. 테스트 메시지를 보냈으니 확인해보세요.", ephemeral=True)


@tree.command(name="인증로그해제", description="인증 웹훅 해제 (관리자)")
@app_commands.checks.has_permissions(manage_guild=True)
async def remove_verify_log(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    try:
        removed = await webapi.webhook_remove(guild.id)
    except webapi.ApiError as e:
        await interaction.response.send_message(f"❌ 웹 서버 연결 실패: {e}", ephemeral=True)
        return
    if removed:
        await interaction.response.send_message("✅ 인증 로그 웹훅을 해제했어요.", ephemeral=True)
    else:
        await interaction.response.send_message("설정된 웹훅이 없어요.", ephemeral=True)


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
        log.exception("slash error: %s", error)
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
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
