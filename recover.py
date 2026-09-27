"""서버 테러 대비 복구키 시스템.

- 봇 초대 시: 복구키 생성 → 초대한 사람 DM으로 전달
- 인증 시: guilds.join 토큰이 복구키(서버) 앞으로 누적 저장 (webverify.save_tokens)
- 테러 후: 새 서버에서 /복구 키:<복구키> → 백업된 멤버들을 토큰으로 재초대
"""

import asyncio
import logging
import secrets
import time

import aiohttp
import discord

from webverify import API, HEADERS, db, env

log = logging.getLogger("recover")

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 헷갈리는 문자 제외


def new_key() -> str:
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3))


def ensure_key(guild_id: int, created_by: str) -> tuple[str, bool]:
    """(복구키, 새로만듦여부). 기존 키 있으면 그대로 반환."""
    con = db()
    try:
        row = con.execute(
            "SELECT key FROM recovery_keys WHERE guild_id=?", (str(guild_id),)
        ).fetchone()
        if row:
            return row[0], False
        key = new_key()
        con.execute(
            "INSERT INTO recovery_keys(guild_id, key, created_by, created_at) VALUES(?,?,?,?)",
            (str(guild_id), key, str(created_by), int(time.time())),
        )
        con.commit()
        return key, True
    finally:
        con.close()


def key_to_guild(key: str) -> int | None:
    con = db()
    try:
        row = con.execute(
            "SELECT guild_id FROM recovery_keys WHERE key=?", (key.strip().upper(),)
        ).fetchone()
        return int(row[0]) if row else None
    finally:
        con.close()


def backup_count(guild_id: int) -> int:
    con = db()
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM oauth_tokens WHERE guild_id=?", (str(guild_id),)
        ).fetchone()
        return row[0] if row else 0
    finally:
        con.close()


async def find_inviter(guild: discord.Guild, bot_id: int) -> discord.User | discord.Member | None:
    """감사로그에서 봇 초대한 사람 특정. 실패 시 None."""
    me = guild.me
    if me is None or not me.guild_permissions.view_audit_log:
        return None
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.bot_add, limit=5):
            if entry.target and entry.target.id == bot_id:
                return entry.user
        return None
    except (discord.Forbidden, discord.HTTPException):
        return None


async def refresh_access(s: aiohttp.ClientSession, refresh: str) -> dict | None:
    if not refresh:
        return None
    data = {
        "client_id": env("DISCORD_CLIENT_ID"),
        "client_secret": env("DISCORD_CLIENT_SECRET"),
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }
    async with s.post(f"{API}/oauth2/token", data=data) as r:
        if r.status != 200:
            return None
        return await r.json()


async def add_member(
    s: aiohttp.ClientSession, target_guild: int, user_id: str, access: str
) -> str:
    """멤버 1명 추가. 결과: ok / already / dead / rate / fail"""
    url = f"{API}/guilds/{target_guild}/members/{user_id}"
    headers = {**HEADERS, "Authorization": f"Bot {env('DISCORD_TOKEN')}"}
    async with s.put(url, headers=headers, json={"access_token": access}) as r:
        if r.status in (201, 204):
            return "already" if r.status == 204 else "ok"
        if r.status in (401, 403):
            return "dead"
        if r.status == 429:
            try:
                retry = float((await r.json()).get("retry_after", 5))
            except Exception:
                retry = 5
            await asyncio.sleep(min(retry, 60))
            return "rate"
        return "fail"


async def run_restore(
    target_guild: int,
    source_guild: int,
    progress_cb,
) -> dict:
    """백업 멤버 전원 복구. progress_cb(done, total) 호출. 결과 dict 반환."""
    from webverify import get_backed_users, save_tokens

    users = get_backed_users(source_guild)
    total = len(users)
    res = {"total": total, "ok": 0, "already": 0, "dead": 0, "fail": 0}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as s:
        for i, (uid, access, refresh, exp) in enumerate(users, 1):
            if int(exp or 0) < time.time() + 60:
                tok = await refresh_access(s, refresh)
                if tok and tok.get("access_token"):
                    access = tok["access_token"]
                    save_tokens(uid, source_guild, access,
                                tok.get("refresh_token", refresh), tok.get("expires_in", 0))
                else:
                    res["dead"] += 1
                    await progress_cb(i, total)
                    continue
            for _ in range(3):  # rate면 최대 3회 재시도
                st = await add_member(s, target_guild, uid, access)
                if st != "rate":
                    break
            res[st if st in res else "fail"] += 1
            await asyncio.sleep(1.2)  # 멤버추가 레이트리밋 준수
            await progress_cb(i, total)
    return res
