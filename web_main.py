"""웹 인증 서버 전용 진입점 (별도 무료 웹 호스트용).

봇(디스호스트)과 파일을 공유할 수 없어서 웹은 이 파일 하나로 분리 실행한다.
discord.py 가 필요 없으므로 aiohttp + python-dotenv 만 설치하면 뜬다.

실행: python web_main.py
환경변수: WEB_PORT(없으면 SERVER_PORT, 그 외 8000), WEB_PUBLIC_URL, WEB_SECRET,
          DISCORD_CLIENT_ID/SECRET — 그리고 봇과 같은 DB/설정 값.
DISABLE_BOT=1 을 줘도 되지만 이 진입점은 애초에 봇을 import 하지 않는다.
"""
import asyncio
import logging
import os

from dotenv import load_dotenv
from aiohttp import web

import webverify

load_dotenv()
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("web")


async def main() -> None:
    for e in webverify.validate():
        log.warning("웹 인증 미설정: %s", e)
    app = webverify.create_app()
    port = int(
        os.getenv("WEB_PORT", "").strip()
        or os.getenv("SERVER_PORT", "").strip()
        or "8000"
    )
    runner = web.AppRunner(app)
    await runner.setup()
    # 외부 프록시/nginx 가 붙으려면 0.0.0.0 으로 열어야 한다 (localhost 금지)
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("웹 인증 서버 실행: 포트 %s (/health, /callback)", port)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹ 중지 신호를 받아 종료했습니다.", flush=True)
    except BaseException as exc:  # noqa: BLE001 — 마지막 줄에 원인을 남긴다
        import traceback

        print(traceback.format_exc(), flush=True)
        print(f"❌ FATAL-LAST: {type(exc).__name__} :: {exc}", flush=True)
        raise SystemExit(1)
