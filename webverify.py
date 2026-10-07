"""웹 OAuth 인증 (이메일 + 중복IP 검사) + Discord REST 역할 지급.

흐름: 패널 링크 → Discord OAuth 승인(identify+email) → /callback →
  토큰 교환 → 유저조회 → 이메일인증여부/중복IP 검사 → 역할 지급 → 결과 페이지.
봇 토큰으로 REST 호출하므로 봇 상시접속 없이도 역할 지급 가능.
인증 기록은 verifications.db(SQLite)에 저장.
"""

import hashlib
import hmac
import html
import json
import logging
import os
import sqlite3
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote

import aiohttp
from aiohttp import web

log = logging.getLogger("webverify")

BASE_DIR = Path(__file__).parent
DB_FILE = BASE_DIR / "verifications.db"
WEBHOOK_FILE = BASE_DIR / "webhooks.json"

API = "https://discord.com/api/v10"
SCOPES = "identify email guilds.join"


# ---------- 설정 ----------
def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def redirect_uri() -> str:
    return env("WEB_PUBLIC_URL").rstrip("/") + "/callback"


def validate() -> list[str]:
    errors = []
    for k in ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "WEB_PUBLIC_URL", "WEB_SECRET"):
        if not env(k):
            errors.append(f"{k}가 .env에 없습니다.")
    return errors


# ---------- state 서명 (역할 위조 방지) ----------
def sign_state(guild_id: int, role_id: int) -> str:
    msg = f"{guild_id}.{role_id}"
    sig = hmac.new(env("WEB_SECRET").encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{msg}.{sig}"


def parse_state(state: str) -> tuple[int, int] | None:
    try:
        guild_s, role_s, sig = state.split(".")
        expect = hmac.new(
            env("WEB_SECRET").encode(), f"{guild_s}.{role_s}".encode(), hashlib.sha256
        ).hexdigest()[:32]
        if not hmac.compare_digest(expect, sig):
            return None
        guild_id, role_id = int(guild_s), int(role_s)
    except (ValueError, AttributeError):
        return None
    allowed = [g.strip() for g in env("ALLOWED_GUILDS", env("GUILD_ID")).split(",") if g.strip()]
    if allowed and str(guild_id) not in allowed:
        return None
    return guild_id, role_id


def build_authorize_url(guild_id: int, role_id: int) -> str:
    q = urllib.parse.urlencode(
        {
            "client_id": env("DISCORD_CLIENT_ID"),
            "response_type": "code",
            "redirect_uri": redirect_uri(),
            "scope": SCOPES,
            "state": sign_state(guild_id, role_id),
        }
    )
    return f"https://discord.com/oauth2/authorize?{q}"


# ---------- DB ----------
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_FILE)
    con.execute(
        "CREATE TABLE IF NOT EXISTS verifications"
        "(user_id TEXT, guild_id TEXT, email TEXT, ip TEXT, ts INTEGER,"
        " PRIMARY KEY(user_id, guild_id))"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_ip ON verifications(guild_id, ip)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS oauth_tokens"
        "(user_id TEXT, guild_id TEXT, access TEXT, refresh TEXT, expires_at INTEGER,"
        " PRIMARY KEY(user_id, guild_id))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS recovery_keys"
        "(guild_id TEXT PRIMARY KEY, key TEXT, created_by TEXT, created_at INTEGER)"
    )
    return con


def ip_used_by_other(guild_id: int, ip: str, user_id: str) -> str | None:
    """같은 서버+IP로 인증한 다른 계정이 있으면 그 유저ID 반환."""
    con = db()
    try:
        row = con.execute(
            "SELECT user_id FROM verifications WHERE guild_id=? AND ip=? AND user_id!=? LIMIT 1",
            (str(guild_id), ip, str(user_id)),
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def save_record(guild_id: int, user_id: str, email: str, ip: str):
    con = db()
    try:
        con.execute(
            "INSERT OR REPLACE INTO verifications(user_id, guild_id, email, ip, ts)"
            " VALUES(?,?,?,?,?)",
            (str(user_id), str(guild_id), email, ip, int(time.time())),
        )
        con.commit()
    finally:
        con.close()


def alt_accounts(guild_id: int, ip: str, user_id: str, limit: int = 5) -> list[str]:
    """같은 서버+IP 기록이 있는 다른 계정 목록."""
    con = db()
    try:
        rows = con.execute(
            "SELECT user_id FROM verifications WHERE guild_id=? AND ip=? AND user_id!=? LIMIT ?",
            (str(guild_id), ip, str(user_id), limit),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


# ---------- 수집 정보 가공 ----------
WEEK_KO = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]


def snowflake_time(user_id: str) -> datetime:
    return datetime.fromtimestamp(((int(user_id) >> 22) + 1420070400000) / 1000, tz=timezone.utc)


def ko_datetime(dt: datetime) -> str:
    kst = dt.astimezone(timezone.utc).astimezone()  # 서버 로컬 시간
    ampm = "오전" if kst.hour < 12 else "오후"
    h = kst.hour if kst.hour <= 12 else kst.hour - 12
    h = 12 if h == 0 else h
    return f"{kst.year}년 {kst.month}월 {kst.day}일 {WEEK_KO[kst.weekday()]} {ampm} {h}:{kst.minute:02d}"


def ko_rel(dt: datetime) -> str:
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 60:
        return "방금 전"
    m, h, d = s // 60, s // 3600, s // 86400
    if d >= 365:
        return f"{int(d // 365)}년 전"
    if d >= 30:
        return f"{int(d // 30)}달 전"
    if d >= 1:
        return f"{int(d)}일 전"
    if h >= 1:
        return f"{int(h)}시간 전"
    return f"{int(m)}분 전"


def parse_ua(ua: str) -> tuple[str, str]:
    """User-Agent → (브라우저, OS)."""
    b, o = "알 수 없음", "알 수 없음"
    if "SamsungBrowser" in ua:
        b = "Samsung Internet"
    elif "Edg" in ua:
        b = "Edge"
    elif "OPR" in ua or "Opera" in ua:
        b = "Opera"
    elif "Firefox" in ua:
        b = "Firefox"
    elif "Chrome" in ua:
        b = "Chrome"
    elif "Safari" in ua:
        b = "Safari"
    if "Android" in ua:
        o = "Android"
    elif "iPhone" in ua or "iPad" in ua:
        o = "iOS"
    elif "Windows" in ua:
        o = "Windows"
    elif "Mac OS" in ua:
        o = "macOS"
    elif "Linux" in ua:
        o = "Linux"
    if "Mobile" in ua and o == "알 수 없음":
        o = "모바일"
    return b, o


def is_private_ip(ip: str) -> bool:
    return ip == "unknown" or ip.startswith(("127.", "10.", "192.168.", "172.")) or ip == "::1"


async def geo_lookup(s: aiohttp.ClientSession, ip: str) -> tuple[str, str]:
    """(위치, 통신사). 실패 시 ('-', '-')."""
    if is_private_ip(ip):
        return "로컬 테스트", "-"
    try:
        async with s.get(f"https://ipwho.is/{ip}") as r:
            if r.status != 200:
                return "-", "-"
            d = await r.json()
    except Exception:
        return "-", "-"
    if not d.get("success", False):
        return "-", "-"
    loc = ", ".join(p for p in (d.get("country"), d.get("region"), d.get("city")) if p) or "-"
    conn = d.get("connection") or {}
    return loc, conn.get("isp") or "-"


async def guild_meta(s: aiohttp.ClientSession, guild_id: int, role_id: int) -> tuple[str, str]:
    """(서버 인원 수, 역할 멘션용 문자열)."""
    headers = {**HEADERS, "Authorization": f"Bot {env('DISCORD_TOKEN')}"}
    members, role_mention = "?", f"<@&{role_id}>"
    try:
        async with s.get(f"{API}/guilds/{guild_id}?with_counts=true", headers=headers) as r:
            if r.status == 200:
                d = await r.json()
                n = d.get("approximate_member_count")
                members = f"{n}명" if n is not None else "?"
    except Exception:
        pass
    try:
        async with s.get(f"{API}/guilds/{guild_id}/roles", headers=headers) as r:
            if r.status == 200:
                for role in await r.json():
                    if str(role.get("id")) == str(role_id):
                        role_mention = f"<@&{role_id}> ({role.get('name')})"
                        break
    except Exception:
        pass
    return members, role_mention


def build_success_embed(info: dict) -> dict:
    alts = info["alts"]
    alts_text = "없음" if not alts else " ".join(f"<@{u}>" for u in alts)
    desc = (
        f"👤 사용자정보\n<@{info['user_id']}>\n"
        f"`{info['username']}`(Global name: `{info['global_name']}`, ID: `{info['user_id']}`)\n"
        f"🎂 계정 생성일\n`{info['created']}` (`{info['created_rel']}`)\n"
        f"🔑 2단계 인증\n`{info['mfa']}`\n"
        f"🕒 인증 시각\n`{info['now']}` (`{info['now_rel']}`)\n"
        f"🌍 IP 정보\nIP: `{info['ip']}`\n위치: `{info['loc']}`\n통신사: `{info['isp']}`\n"
        f"📱 기기 정보\n브라우저: `{info['browser']}`\n운영체제: `{info['os']}`\n"
        f"🔒 부계정으로 추정되는 계정\n{alts_text}\n"
        f"🎯 서버 인원 / 지급된 역할\n`{info['members']}` · {info['role']}"
    )
    return {
        "title": "✅ 인증 성공",
        "description": desc,
        "color": 0x57F287,
        "footer": {"text": f"로그 ID: {info['log_id']}"},
    }


# ---------- Discord API ----------
HEADERS = {"User-Agent": "DiscordBot (verify-bot, 1.0)"}


async def exchange_code(s: aiohttp.ClientSession, code: str) -> dict | None:
    data = {
        "client_id": env("DISCORD_CLIENT_ID"),
        "client_secret": env("DISCORD_CLIENT_SECRET"),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
    }
    async with s.post(f"{API}/oauth2/token", data=data) as r:
        if r.status != 200:
            log.warning("토큰 교환 실패: HTTP %s %s", r.status, (await r.text())[:200])
            return None
        return await r.json()


def save_tokens(user_id: str, guild_id: int, access: str, refresh: str, expires_in: int):
    """복구용 OAuth 토큰 저장 (access 만료시 refresh로 갱신)."""
    if not access:
        return
    con = db()
    try:
        con.execute(
            "INSERT OR REPLACE INTO oauth_tokens(user_id, guild_id, access, refresh, expires_at)"
            " VALUES(?,?,?,?,?)",
            (str(user_id), str(guild_id), access, refresh or "", int(time.time()) + int(expires_in or 0)),
        )
        con.commit()
    finally:
        con.close()


def get_backed_users(guild_id: int) -> list[tuple]:
    """(user_id, access, refresh, expires_at) 목록."""
    con = db()
    try:
        return con.execute(
            "SELECT user_id, access, refresh, expires_at FROM oauth_tokens WHERE guild_id=?",
            (str(guild_id),),
        ).fetchall()
    finally:
        con.close()


async def fetch_user(s: aiohttp.ClientSession, access_token: str) -> dict | None:
    async with s.get(f"{API}/users/@me", headers={"Authorization": f"Bearer {access_token}"}) as r:
        if r.status != 200:
            log.warning("유저 조회 실패: HTTP %s", r.status)
            return None
        return await r.json()


async def add_role(s: aiohttp.ClientSession, guild_id: int, user_id: str, role_id: int) -> tuple[bool, str]:
    """REST로 역할 지급. (성공여부, 사유) 반환."""
    url = f"{API}/guilds/{guild_id}/members/{user_id}/roles/{role_id}"
    headers = {**HEADERS, "Authorization": f"Bot {env('DISCORD_TOKEN')}"}
    async with s.put(url, headers=headers) as r:
        if r.status == 204:
            return True, "ok"
        try:
            code = (await r.json()).get("code", r.status)
        except Exception:
            code = r.status
        # 10007=서버에 없음, 10011=롤 없음, 10004=서버 없음, 50013=권한부족
        reasons = {
            0: "봇 토큰이 유효하지 않습니다. 관리자가 토큰을 교체해야 합니다.",
            10007: "서버에 입장하지 않았습니다. 먼저 서버에 들어와주세요.",
            10011: "역할이 삭제됐습니다. 관리자에게 문의하세요.",
            10004: "서버 설정을 확인해주세요. (관리자 문의)",
            50013: "봇 권한이 부족합니다. (관리자 문의)",
        }
        return False, reasons.get(code, f"역할 지급 실패 (code {code}). 관리자에게 문의하세요.")


async def notify_webhook(embed: dict):
    try:
        hooks = json.loads(WEBHOOK_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return
    for url in hooks.values():
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as s:
                async with s.post(url, json={"embeds": [embed]}):
                    pass
        except Exception as e:
            log.warning("인증 웹훅 실패: %s", e)


# ---------- HTML ----------
CSS = """
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css');
:root{--txt:#eef0f8;--sub:#98a0bd;--line:rgba(255,255,255,.09);--card:rgba(17,20,36,.72)}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;padding:24px;color:var(--txt);
 font-family:'Pretendard',-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;
 background:
  radial-gradient(900px 520px at 10% -12%,rgba(88,101,242,.40),transparent 60%),
  radial-gradient(780px 500px at 94% 6%,rgba(139,92,246,.30),transparent 62%),
  radial-gradient(760px 760px at 50% 116%,rgba(56,189,248,.18),transparent 62%),
  linear-gradient(165deg,#080a16 0%,#0b0e1a 48%,#070912 100%);
 background-attachment:fixed;-webkit-font-smoothing:antialiased;
 display:flex;align-items:center;justify-content:center}
.orb{position:fixed;border-radius:50%;filter:blur(90px);opacity:.42;pointer-events:none;z-index:0}
.orb.a{width:420px;height:420px;left:-130px;top:-130px;background:#5865f2;
 animation:drift 17s ease-in-out infinite}
.orb.b{width:400px;height:400px;right:-120px;bottom:-140px;background:#8b5cf6;
 animation:drift 21s ease-in-out infinite reverse}
@keyframes drift{0%,100%{transform:translate3d(0,0,0)}50%{transform:translate3d(46px,-34px,0)}}
.stage{position:relative;z-index:1;width:100%;max-width:430px;
 animation:rise .55s cubic-bezier(.2,.8,.2,1) both}
@keyframes rise{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:none}}
.card{position:relative;overflow:hidden;border-radius:26px;padding:40px 34px 30px;
 text-align:center;
 background:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.02)),var(--card);
 backdrop-filter:blur(20px) saturate(150%);
 -webkit-backdrop-filter:blur(20px) saturate(150%);
 border:1px solid var(--line);
 box-shadow:0 34px 90px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,255,255,.09)}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
 background:linear-gradient(90deg,transparent,rgba(255,255,255,.55),transparent)}
.card::after{content:'';position:absolute;top:-150px;left:50%;width:280px;height:280px;
 transform:translateX(-50%);border-radius:50%;filter:blur(14px);opacity:.34;pointer-events:none;
 background:radial-gradient(circle,#5865f2,transparent 70%)}
.card.ok::after{background:radial-gradient(circle,#3ba55d,transparent 70%)}
.card.no::after{background:radial-gradient(circle,#ed4245,transparent 70%)}
.badge{position:relative;z-index:1;width:86px;height:86px;margin:0 auto 18px;border-radius:50%;
 display:flex;align-items:center;justify-content:center;font-size:38px;line-height:1;
 background:linear-gradient(145deg,rgba(255,255,255,.15),rgba(255,255,255,.04));
 border:1px solid rgba(255,255,255,.17);
 box-shadow:0 14px 34px rgba(0,0,0,.5),0 0 0 7px rgba(88,101,242,.12)}
.badge.ok{color:#57f287;box-shadow:0 14px 34px rgba(0,0,0,.5),0 0 0 7px rgba(87,242,135,.14)}
.badge.no{color:#ff6b6b;box-shadow:0 14px 34px rgba(0,0,0,.5),0 0 0 7px rgba(237,66,69,.16)}
.badge img{width:100%;height:100%;object-fit:cover;border-radius:50%;display:block}
.eyebrow{position:relative;z-index:1;margin:0 0 9px;font-size:10.5px;font-weight:800;
 letter-spacing:.26em;color:var(--sub);text-transform:uppercase}
h1{position:relative;z-index:1;margin:0 0 14px;font-size:27px;font-weight:800;
 letter-spacing:-.02em;word-break:keep-all}
.line{position:relative;z-index:1;height:1px;margin:0 0 16px;
 background:linear-gradient(90deg,transparent,rgba(255,255,255,.2),transparent)}
p.msg{position:relative;z-index:1;margin:0;color:#b9c0da;font-size:14.5px;line-height:1.75;
 word-break:keep-all}
p.msg.ok{color:#7beda4}
p.msg.no{color:#ff8a8a}
.steps{position:relative;z-index:1;list-style:none;display:flex;gap:7px;margin:22px 0 0;padding:0}
.steps li{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;padding:10px 4px;
 font-size:11px;color:#98a0bd;background:rgba(255,255,255,.045);
 border:1px solid var(--line);border-radius:13px}
.steps b{width:21px;height:21px;border-radius:50%;display:flex;align-items:center;
 justify-content:center;font-size:11px;color:#fff;
 background:linear-gradient(135deg,#5865f2,#8b5cf6);box-shadow:0 4px 12px rgba(88,101,242,.5)}
a.btn{position:relative;z-index:1;display:flex;align-items:center;justify-content:center;gap:9px;
 margin-top:24px;padding:15px 20px;border-radius:15px;text-decoration:none;color:#fff;
 font-size:16px;font-weight:700;letter-spacing:-.01em;
 background:linear-gradient(135deg,#5865f2 0%,#7b5cf5 55%,#8b5cf6 100%);
 box-shadow:0 16px 34px rgba(88,101,242,.45),inset 0 1px 0 rgba(255,255,255,.3);
 transition:transform .16s ease,box-shadow .16s ease,filter .16s ease}
a.btn:hover{transform:translateY(-2px);filter:brightness(1.07);
 box-shadow:0 20px 42px rgba(88,101,242,.6),inset 0 1px 0 rgba(255,255,255,.35)}
a.btn:active{transform:translateY(0)}
a.btn .arrow{transition:transform .16s ease}
a.btn:hover .arrow{transform:translateX(5px)}
.note{position:relative;z-index:1;display:flex;align-items:center;justify-content:center;gap:7px;
 margin-top:18px;padding:9px 15px;border-radius:999px;font-size:12.5px;color:#98a0bd;
 background:rgba(255,255,255,.05);border:1px solid var(--line)}
.foot{position:relative;z-index:1;margin:16px 0 0;font-size:11px;color:#6e7593;letter-spacing:.05em}
@media (max-width:420px){.card{padding:34px 22px 26px}h1{font-size:24px}.steps li{font-size:10px}}
"""


def page(title: str, msg: str, ok: bool) -> web.Response:
    cls = "ok" if ok else "no"
    mark = "✅" if ok else "❌"
    eyebrow = "Verification Success" if ok else "Verification Failed"
    return web.Response(
        text=(
            "<!doctype html><html lang=ko><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<meta name=theme-color content='#0b0e1a'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
            "<div class='orb a'></div><div class='orb b'></div><div class='stage'>"
            f"<section class='card {cls}'>"
            f"<div class='badge {cls}'>{mark}</div>"
            f"<p class='eyebrow'>{eyebrow}</p>"
            f"<h1>{html.escape(title)}</h1>"
            "<div class='line'></div>"
            f"<p class='msg {cls}'>{msg}</p>"
            "<div class='note'>🔒 안전한 HTTPS 연결</div>"
            "<p class='foot'>창을 닫고 디스코드로 돌아가세요</p>"
            "</section></div></body></html>"
        ),
        content_type="text/html",
    )


guild_name_fn = None  # bot.py에서 설정: (guild_id) -> 서버명 | None (봇 캐시)
bot_status_fn = None  # bot.py에서 설정: () -> {"bot": ready/down, "guilds": N}


async def resolve_guild(s: aiohttp.ClientSession, guild_id: int) -> tuple[str | None, str | None]:
    """(서버명, 아이콘URL). 봇 캐시 우선, 실패 시 REST."""
    if guild_name_fn:
        try:
            name = guild_name_fn(guild_id)
            if name:
                return name, None
        except Exception:
            pass
    try:
        headers = {**HEADERS, "Authorization": f"Bot {env('DISCORD_TOKEN')}"}
        async with s.get(f"{API}/guilds/{guild_id}", headers=headers) as r:
            if r.status != 200:
                log.warning("서버명 조회 실패: guild=%s HTTP %s", guild_id, r.status)
                return None, None
            d = await r.json()
            icon = d.get("icon")
            icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon}.png?size=128" if icon else None
            return d.get("name"), icon_url
    except Exception:
        return None, None


def client_ip(request: web.Request) -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote or "unknown"


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_webhook_url(url: str) -> bool:
    """discord.com / discordapp.com (+ptb/canary) 의 /api/webhooks/ URL이면 통과."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return False
    if p.scheme != "https":
        return False
    host = p.netloc.lower()
    if "discord.com" not in host and "discordapp.com" not in host:
        return False
    return "/api/webhooks/" in p.path


async def post_webhook(url: str, payload: dict) -> tuple[bool, str]:
    """웹훅 POST. (성공여부, 상세) 반환."""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"User-Agent": "DiscordBot (verify-bot, 1.0)"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.post(url, json=payload) as r:
                if r.status in (200, 204):
                    return True, f"HTTP {r.status}"
                body = (await r.text())[:200]
                detail = f"HTTP {r.status} {body}"
                log.warning("웹훅 전송 실패: %s", detail)
                return False, detail
    except Exception as e:
        detail = str(e)[:200]
        log.warning("웹훅 오류: %s", e)
        return False, detail


# ---------- 내부 API (봇 → 웹, X-Api-Key = WEB_SECRET) ----------
def api_auth(request: web.Request) -> bool:
    secret = env("WEB_SECRET")
    return bool(secret) and hmac.compare_digest(request.headers.get("X-Api-Key", ""), secret)


def need_auth(request: web.Request) -> web.Response | None:
    if not api_auth(request):
        return web.json_response({"detail": "unauthorized"}, status=401)
    return None


async def api_ping(request: web.Request) -> web.Response:
    if (r := need_auth(request)) is not None:
        return r
    return web.json_response({"ok": True})


async def api_webhook_test(request: web.Request) -> web.Response:
    if (r := need_auth(request)) is not None:
        return r
    try:
        body = await request.json()
        guild_id, url = int(body["guild"]), str(body.get("url", "")).strip()
    except (ValueError, KeyError, TypeError):
        return web.json_response({"detail": "guild/url 필요"}, status=400)
    if not is_webhook_url(url):
        return web.json_response({"ok": False, "detail": "웹훅 URL 형식 아님"}, status=200)
    ok, detail = await post_webhook(
        url,
        {"embeds": [{"title": "🔗 인증 로그 연결됨", "description": "앞으로 인증 성공 시 여기에 로그가 전송됩니다.", "color": 0x57F287}]},
    )
    if ok:
        hooks = _load_json(WEBHOOK_FILE)
        hooks[str(guild_id)] = url
        _save_json(WEBHOOK_FILE, hooks)
    return web.json_response({"ok": ok, "detail": detail})


async def api_webhook_remove(request: web.Request) -> web.Response:
    if (r := need_auth(request)) is not None:
        return r
    try:
        guild_id = int((await request.json())["guild"])
    except (ValueError, KeyError, TypeError):
        return web.json_response({"detail": "guild 필요"}, status=400)
    hooks = _load_json(WEBHOOK_FILE)
    removed = str(guild_id) in hooks
    if removed:
        del hooks[str(guild_id)]
        _save_json(WEBHOOK_FILE, hooks)
    return web.json_response({"removed": removed})


async def api_backup_count(request: web.Request) -> web.Response:
    if (r := need_auth(request)) is not None:
        return r
    try:
        guild_id = int(request.query["guild"])
    except (KeyError, ValueError):
        return web.json_response({"detail": "guild 필요"}, status=400)
    con = db()
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM oauth_tokens WHERE guild_id=?", (str(guild_id),)
        ).fetchone()
        return web.json_response({"count": row[0] if row else 0})
    finally:
        con.close()


async def api_backup_users(request: web.Request) -> web.Response:
    if (r := need_auth(request)) is not None:
        return r
    try:
        guild_id = int(request.query["guild"])
    except (KeyError, ValueError):
        return web.json_response({"detail": "guild 필요"}, status=400)
    users = [
        {"user_id": u, "access": a, "refresh": rf, "expires_at": e}
        for (u, a, rf, e) in get_backed_users(guild_id)
    ]
    return web.json_response({"users": users})


async def api_token_save(request: web.Request) -> web.Response:
    if (r := need_auth(request)) is not None:
        return r
    try:
        body = await request.json()
        save_tokens(
            str(body["user_id"]), int(body["guild"]),
            str(body.get("access", "")), str(body.get("refresh", "")),
            int(body.get("expires_in", 0)),
        )
    except (ValueError, KeyError, TypeError) as e:
        return web.json_response({"detail": f"입력 오류: {e}"}, status=400)
    return web.json_response({"ok": True})


# ---------- 라우트 ----------
async def index(request: web.Request) -> web.Response:
    try:
        guild_id = int(request.query["guild"])
        role_id = int(request.query["role"])
    except (KeyError, ValueError):
        return page("잘못된 접근", "서버의 인증 패널 버튼으로 들어와주세요.", False)
    if parse_state(sign_state(guild_id, role_id)) != (guild_id, role_id):
        return page("설정 오류", "서버 설정이 올바르지 않습니다. 관리자에게 문의하세요.", False)
    url = build_authorize_url(guild_id, role_id)
    # 1순위: 패널 링크에 박힌 서버명/아이콘 (봇이 직접 전달, API 불필요)
    given_name = unquote(request.query.get("g", ""))[:50]
    given_icon = request.query.get("i", "")
    if given_name:
        name = given_name
        icon = (
            f"https://cdn.discordapp.com/icons/{guild_id}/{given_icon}.png?size=128"
            if len(given_icon) == 32 and all(c in "0123456789abcdef" for c in given_icon.lower())
            else None
        )
    else:
        # 2순위: 봇 캐시 → Discord API 조회 (구 패널 호환)
        timeout = aiohttp.ClientTimeout(total=8)
        name, icon = None, None
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as s:
                name, icon = await resolve_guild(s, guild_id)
        except Exception:
            pass
    title = html.escape(name) if name else "서버 인증"
    visual = f"<img src='{icon}' alt=''>" if icon else "🔐"
    return web.Response(
        text=(
            "<!doctype html><html lang=ko><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<meta name=theme-color content='#0b0e1a'>"
            f"<title>{title}</title><style>{CSS}</style></head><body>"
            "<div class='orb a'></div><div class='orb b'></div><div class='stage'>"
            "<section class='card'>"
            f"<div class='badge'>{visual}</div>"
            "<p class='eyebrow'>Discord Verification</p>"
            f"<h1>{title}</h1>"
            "<div class='line'></div>"
            "<p class='msg'>본인 확인을 위해 아래 버튼을 눌러주세요.<br>"
            "인증이 끝나면 바로 서버 활동을 시작할 수 있습니다.</p>"
            "<ol class='steps'><li><b>1</b>인증 버튼</li>"
            "<li><b>2</b>Discord 로그인</li><li><b>3</b>역할 지급</li></ol>"
            f"<a class='btn' href='{url}'>인증하기 <span class='arrow'>→</span></a>"
            "<div class='note'>🔒 Discord 공식 로그인 · 비밀번호를 요구하지 않습니다</div>"
            "</section></div></body></html>"
        ),
        content_type="text/html",
    )


async def callback(request: web.Request) -> web.Response:
    code = request.query.get("code", "")
    parsed = parse_state(request.query.get("state", ""))
    if not code or not parsed:
        return page("인증 실패", "유효하지 않은 요청입니다. 패널 버튼으로 다시 시도해주세요.", False)
    guild_id, role_id = parsed
    ip = client_ip(request)

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as s:
        tok = await exchange_code(s, code)
        token = (tok or {}).get("access_token")
        if not token:
            return page("인증 실패", "Discord 승인에 실패했습니다. 다시 시도해주세요.", False)
        me = await fetch_user(s, token)
        if not me:
            return page("인증 실패", "계정 정보를 가져오지 못했습니다. 다시 시도해주세요.", False)

        user_id = str(me["id"])
        username = f"{me.get('username', '')}#{me.get('discriminator', '0')}"
        email = me.get("email") or ""
        email_ok = bool(me.get("verified"))

        if not email_ok:
            await notify_webhook(
                {
                    "title": "❌ 인증 실패 (이메일 미인증)",
                    "color": 0xED4245,
                    "fields": [
                        {"name": "유저", "value": f"{username} (`{user_id}`)", "inline": False},
                        {"name": "IP", "value": ip, "inline": True},
                    ],
                }
            )
            return page(
                "인증 실패",
                "Discord 계정의 이메일 인증이 필요합니다.<br>"
                "디스코드 설정 → 계정에서 이메일을 인증하고 다시 시도해주세요.",
                False,
            )

        dup = ip_used_by_other(guild_id, ip, user_id)
        if dup:
            await notify_webhook(
                {
                    "title": "❌ 인증 실패 (중복 IP)",
                    "color": 0xED4245,
                    "fields": [
                        {"name": "유저", "value": f"{username} (`{user_id}`)", "inline": False},
                        {"name": "IP", "value": f"{ip} (기존 `{dup}`와 동일)", "inline": False},
                    ],
                }
            )
            return page(
                "인증 실패",
                "이 접속 기록으로는 이미 다른 계정이 인증됐습니다.<br>"
                "본인 계정으로 문의해주세요.",
                False,
            )

        ok, reason = await add_role(s, guild_id, user_id, role_id)
        if not ok:
            return page("인증 실패", reason, False)

        save_record(guild_id, user_id, email, ip)
        save_tokens(user_id, guild_id, token, (tok or {}).get("refresh_token", ""), (tok or {}).get("expires_in", 0))
        created = snowflake_time(user_id)
        now = datetime.now(timezone.utc)
        members, role_text = await guild_meta(s, guild_id, role_id)
        loc, isp = await geo_lookup(s, ip)
        browser, osname = parse_ua(request.headers.get("User-Agent", ""))
        await notify_webhook(
            build_success_embed(
                {
                    "user_id": user_id,
                    "username": me.get("username") or "-",
                    "global_name": me.get("global_name") or me.get("username") or "-",
                    "created": ko_datetime(created),
                    "created_rel": ko_rel(created),
                    "mfa": "ON" if me.get("mfa_enabled") else "OFF",
                    "now": ko_datetime(now),
                    "now_rel": ko_rel(now),
                    "ip": ip,
                    "loc": loc,
                    "isp": isp,
                    "browser": browser,
                    "os": osname,
                    "alts": alt_accounts(guild_id, ip, user_id),
                    "members": members,
                    "role": role_text,
                    "log_id": str(uuid.uuid4()),
                }
            )
        )
        log.info("web verified: %s guild=%s", user_id, guild_id)
        # role_text 는 "<@&id> (역할명)" 형태 → 웹에는 역할명만 표시
        role_label = role_text.split("(", 1)[1].rstrip(")") if "(" in role_text else ""
        if role_label:
            ok_msg = (
                f"`{html.escape(role_label)}` 역할이 지급됐습니다.<br>"
                "이제 서버의 모든 채널을 이용할 수 있어요."
            )
        else:
            ok_msg = "역할이 지급됐습니다. 즐거운 활동 되세요!"
        return page("인증 완료", ok_msg, True)


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def status(_: web.Request) -> web.Response:
    info: dict = {"web": "ok"}
    if bot_status_fn:
        try:
            info.update(bot_status_fn())
        except Exception:
            info["bot"] = "unknown"
    return web.json_response(info)


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/callback", callback)
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_get("/api/ping", api_ping)
    app.router.add_post("/api/webhook-test", api_webhook_test)
    app.router.add_post("/api/webhook-remove", api_webhook_remove)
    app.router.add_get("/api/backup-count", api_backup_count)
    app.router.add_get("/api/backup-users", api_backup_users)
    app.router.add_post("/api/token-save", api_token_save)
    return app
