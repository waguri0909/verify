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


async def web_status() -> dict:
    """웹(Render) 쪽 로그 상태: 등록 웹훅 수 / 대기열 적체 / 마지막 전송 결과."""
    return await _req("GET", "/api/ping")


async def webhook_test(guild_id: int, url: str) -> tuple[bool, str, bool]:
    """(성공여부, 상세, URL 저장됨 여부) 반환.

    저장됨 여부는 테스트가 429/5xx 같은 일시 오류로 막혔을 때 True →
    URL 형식은 맞으므로 등록해 두고 실제 인증 때 다시 시도한다.
    """
    d = await _req("POST", "/api/webhook-test", json={"guild": guild_id, "url": url})
    return bool(d.get("ok")), str(d.get("detail", "")), bool(d.get("saved"))


async def webhook_remove(guild_id: int) -> bool:
    return bool((await _req("POST", "/api/webhook-remove", json={"guild": guild_id})).get("removed"))


async def webhook_save(guild_id: int, url: str) -> bool:
    """URL만 저장한다. 전송 테스트는 봇 프로세스에서 이미 보냈으므로 웹은 스킵한다.

    Render IP 는 디스코드 웹훅 POST 제한(429)을 받는 경우가 있어
    웹쪽에서 테스트하면 항상 실패로 보인다.
    """
    d = await _req(
        "POST", "/api/webhook-test",
        json={"guild": guild_id, "url": url, "skip_test": True},
    )
    return bool(d.get("saved"))


async def queue_take() -> list[dict]:
    """봇이 대신 전송할 로그를 모두 가져온다."""
    d = await _req("GET", "/api/queue")
    items = d.get("items", [])
    return items if isinstance(items, list) else []


async def queue_put(item: dict) -> None:
    """전송에 다시 실패한 로그를 대기열로 돌려놓는다."""
    await _req("POST", "/api/queue", json=item)


async def queue_clear() -> int:
    """대기열을 비우고 지운 개수를 돌려준다."""
    return int((await _req("POST", "/api/queue-clear")).get("cleared", 0))


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
