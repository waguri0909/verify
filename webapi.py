"""Render 웹 API 클라이언트 (봇 → 웹).

봇(디스호스트)과 웹(Render)이 파일을 공유할 수 없어서
토큰·웹훅 같은 공유 데이터는 이 API로 주고받음.
인증: X-Api-Key 헤더 = WEB_SECRET. 베이스 = WEB_PUBLIC_URL.
"""

import aiohttp

from webverify import env

TIMEOUT = aiohttp.ClientTimeout(total=70)  # Render 슬립 깨우는 시간 감안


class ApiError(Exception):
    pass


def _base() -> str:
    return env("WEB_PUBLIC_URL").rstrip("/")


async def _req(method: str, path: str, **kw) -> dict:
    base, key = _base(), env("WEB_SECRET")
    if not base or not key:
        raise ApiError("WEB_PUBLIC_URL / WEB_SECRET 미설정 (.env 확인)")
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.request(
                method, base + path, headers={"X-Api-Key": key}, **kw
            ) as r:
                try:
                    data = await r.json()
                except Exception:
                    data = {"detail": (await r.text())[:200]}
                if r.status != 200:
                    raise ApiError(data.get("detail") or f"HTTP {r.status}")
                if not isinstance(data, dict):
                    raise ApiError("웹 응답 형식 오류")
                return data
    except ApiError:
        raise
    except Exception as e:
        raise ApiError(f"웹 연결 실패(슬립 가능, 잠시 후 재시도): {e}") from e


async def api_ping() -> bool:
    return (await _req("GET", "/api/ping")).get("ok") is True


async def webhook_test(guild_id: int, url: str) -> tuple[bool, str]:
    d = await _req("POST", "/api/webhook-test", json={"guild": guild_id, "url": url})
    return bool(d.get("ok")), str(d.get("detail", ""))


async def webhook_remove(guild_id: int) -> bool:
    return bool((await _req("POST", "/api/webhook-remove", json={"guild": guild_id})).get("removed"))


async def backup_count(guild_id: int) -> int:
    d = await _req("GET", "/api/backup-count", params={"guild": guild_id})
    return int(d.get("count", 0))


async def backup_users(guild_id: int) -> list[dict]:
    d = await _req("GET", "/api/backup-users", params={"guild": guild_id})
    users = d.get("users", [])
    return users if isinstance(users, list) else []


async def token_save(user_id: str, guild_id: int, access: str, refresh: str, expires_in: int) -> bool:
    d = await _req(
        "POST", "/api/token-save",
        json={"user_id": user_id, "guild": guild_id, "access": access,
              "refresh": refresh, "expires_in": expires_in},
    )
    return bool(d.get("ok"))
