"""호스팅 패널용 진입점 래퍼 (STARTUP_FILE 이 main.py 로 고정된 서버용).

실제 구현은 bot.py 에 있다. 시작 파일을 서버 패널에서 수정할 수 없을 때
이 파일 하나가 실행 진입점 역할을 해준다.

실행: python main.py  (== python bot.py)
"""
import asyncio

from bot import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # 패널의 Stop/재시작이 보내는 중지 신호. 스택트레이스 대신 한 줄로 정리.
        print("\n⏹ 중지 신호를 받아 종료했습니다.", flush=True)
