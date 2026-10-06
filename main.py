"""호스팅 패널용 진입점 래퍼 (STARTUP_FILE 이 main.py 로 고정된 서버용).

실제 구현은 bot.py 에 있다. 시작 파일을 서버 패널에서 수정할 수 없을 때
이 파일 하나가 실행 진입점 역할을 해준다.

실행: python main.py  (== python bot.py)
"""
import asyncio

from bot import main

if __name__ == "__main__":
    asyncio.run(main())
