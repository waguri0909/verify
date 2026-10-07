# Discord 종합봇 (웹 인증 + 티켓, Python)

패널 **[웹에서 인증하기]** → Discord 승인 → 웹사이트에서 **이메일 인증여부 + 중복IP 검사** → 역할 자동 지급.
패널 **[🛒 구매 문의]** / **[❓ 일반 / 파트너 문의]** → 1인 1개 전용 채널 → **[닫기]** → **[삭제]/[재오픈]**.

## 0. 구조
- `bot.py` — 디스코드 봇 + 웹사이트를 한 프로세스로 실행 (무료 호스팅 1서비스용)
- `ticket.py` — 티켓 시스템 (패널/개설/잠금/삭제)
- `webverify.py` — OAuth/검사/역할지급 로직
- `render.yaml` — Render 무료 배포용 블루프린트
- 인증 기록: `verifications.db` (자동생성), 인증 패널 설정: `panels.json`,
  티켓 설정·기록: `tickets.json` (자동생성), 웹훅: `webhooks.json`

## 1. 개발자 포털 설정
1. https://discord.com/developers/applications → 내 앱 → **OAuth2** 페이지
2. **Client ID** 복사, **Client Secret → Reset Secret** → 복사 (`.env`에 필요)
3. **Redirects → Add Another** → `https://내주소/callback` 등록 → Save
   (로컬 테스트면 `http://localhost:8000/callback`. 단 Discord는 https가 아닌 localhost는 허용)
4. Bot 페이지 → **SERVER MEMBERS INTENT ON** (입장 감지용)
5. 봇 초대: OAuth2 → URL Generator → Scopes `bot`+`applications.commands`, 권한 `Manage Roles/Send Messages/Embed Links`
6. 서버에서 인증 롤 만들고 **봇 롤을 그 롤보다 위로**

> 이메일 조회는 `identify`+`email` 스코프로 별도 승인 없이 사용 가능.

## 2. 실행 (로컬)
```powershell
pip install -r requirements.txt
copy .env.example .env
# .env 채우기 (아래 표 참고)
python bot.py   # 봇 + 웹(:8000) 동시 실행
```
- `/인증패널 역할:@인증완료` → 링크 버튼 패널 게시
- 유저가 버튼 → Discord 승인 → 검사 통과 시 역할 지급 + 결과 페이지
- `/티켓패널` → 버튼 **2개**(구매 문의 / 일반·파트너 문의) 패널 게시 (자세한 건 4-2 절)

## 3. 무료 호스팅 (Render, 공개 https 필수)
1. 이 폴더를 GitHub에 올리기
2. https://render.com → New → **Blueprint** → 저장소 선택 (`render.yaml` 자동인식, free)
3. 환경변수에 `DISCORD_TOKEN`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `WEB_PUBLIC_URL` 입력
   (`WEB_SECRET`은 자동생성, `WEB_PORT` 불필요 — Render의 PORT 자동사용)
4. 첫 배포 후 발급된 주소(예: `https://xxx.onrender.com`)를
   - 포털 OAuth2 Redirects에 `https://xxx.onrender.com/callback` 등록
   - Render 환경변수 `WEB_PUBLIC_URL`에 `https://xxx.onrender.com` 입력 → 재배포
5. 주의: 무료 플랜은 15분 무사용 시 슬립 → 첫 인증이 30초쯤 느릴 수 있음.
   24시간 즉시반응이 필요하면 유료 플랜 or 아래 분리 구성을 고려.

## 3-2. 분리 구성 (봇=디스호스트 24시간 + 웹=Render)
Render 슬립 때문에 봇 명령어가 자주 죽는다면:
- **웹(Render)**: 그대로. `DISABLE_BOT=1` 환경변수 추가 → 웹만 실행
- **봇(디스호스트)**: Python 서버 생성 → 이 폴더 파일 업로드
  (`bot.py`, `webverify.py`, `webapi.py`, `recover.py`, `requirements.txt`)
  → 시작 명령 `python bot.py` → 환경변수 설정 후 실행:
  `DISCORD_TOKEN`, `WEB_PUBLIC_URL`(Render 주소), `WEB_SECRET`(Render와 동일),
  `VERIFIED_ROLE_NAME`, `UNVERIFIED_ROLE_NAME`, `DISABLE_WEB=1`
  (+ 선택: `AUTH_CHANNEL_ID`, `LOG_CHANNEL_ID`, `GUILD_ID`)
  ※ `DISCORD_CLIENT_SECRET`은 웹 쪽에만 있으면 됨 (봇 쪽 불필요)
  ※ 디스호스트는 7일마다 연장 클릭 필요, 무료 128MB라 로그で 확인
- 봇↔웹 통신은 `X-Api-Key: WEB_SECRET` 로 인증 (`/api/*`)
- 두 곳 토큰이 같으면 세션 충돌 → 반드시 한쪽은 봇 끄기 (위 플래그)

## 4. 디스코드 명령 (관리자)
- `/인증패널 역할:@롤 채널:#인증 제목:.. 문장1:.. 문장2:..` → 웹인증 링크 패널 게시
- `/인증초기화 유저:@누구` → 인증 해제
- `/인증로그 웹훅:<URL>` → 웹 인증 성공/실패 로그 전송. 성공 시 수집 항목:
  사용자정보(멘션·글로벌명·ID), 계정 생성일(상대시간), 2단계 인증 ON/OFF,
  인증 시각, IP/위치/통신사(무료조회, 실패 시 `-`), 브라우저/OS(접속 UA),
  부계정 추정(같은 IP 기록 계정 멘션), 서버 인원, 지급 역할, 로그 ID(UUID)
- `/인증로그해제` → 웹훅 해제
- `/복구현황 키:<복구키>` → 키에 쌓인 인원 확인
- `/복구 키:<복구키>` → 쌓인 멤버들을 **이 서버**에 재초대 (시간 소요)
- `/티켓패널 채널:#문의 제목:.. 설명:.. 역할:@스태프 카테고리:..` → 티켓 패널 게시
- `/티켓설정 역할:@스태프 카테고리:.. 해제:.. 번호초기화:예|아니오` → 설정 확인/변경
- `/티켓사유 상태:켜기|끄기` → 티켓 열 때 사유 입력창 on/off
- `/티켓닫기 사유:..` → 현재 채널의 티켓을 닫기 (버튼과 동일)

## 4-2. 티켓 사용법
1. `/티켓패널` → **[🛒 구매 문의]** / **[❓ 일반 / 파트너 문의]** 버튼이 붙은 패널 게시
2. 유저가 버튼 → (사유 입력창이 켜져 있으면 모달) → 전용 채널 생성
   - 채널명: `ticket-001-유저이름`, 본문: `🎫 티켓 #001` (**번호 표기**)
   - @everyone 숨김 + 문의자/스태프/봇만 접근, **1인 1개 제한**
3. **[닫기 🔒]** → 채널명 `closed-001-...` 로 바뀌고 문의자가 읽기 전용
   → **[재오픈 ↩️]** / **[삭제 🗑️]**
- 봇 권한: **채널 관리**, **메시지 전송**, **.embed links** 필요
- 스태프 역할을 지정하면 그 역할이 모든 티켓을 관리, 안 지정하면
  채널관리/서버관리 권한 보유자가 관리
- 티켓 번호는 서버 단위로 계속 오릅니다. 0으로 되돌리려면
  `/티켓설정 번호초기화:예`

## 5. 복구키 (테러 대비)
1. 봇을 서버에 초대하면 초대한 사람 DM으로 `XXXX-XXXX-XXXX` 키 발급 (감사로그 특정, 실패 시 서버장)
2. 웹 인증 때 `guilds.join` 승인까지 한 사람이 키 앞으로 자동 누적
3. 테러 후 새 서버에 봇 초대 → `/복구 키:...` → 한 명씩 재초대 (레이트리밋 준수, 약 1.5초/명)
- 승인 안 한 사람·토큰 취소한 사람은 복구 불가 → 재인증 필요
- 키 유출되면 누구나 멤버를 빼갈 수 있으니 절대 공유 금지
- 기존 인증자는 토큰이 없어서 소급 불가 → 패널 다시 게시(`/인증패널`) 후 재인증 필요

## 6. 환경변수 설명
| 키 | 설명 |
|---|---|
| DISCORD_TOKEN | 봇 토큰 (필수) |
| DISCORD_CLIENT_ID / _SECRET | OAuth2용 (웹인증 필수) |
| WEB_PUBLIC_URL | 사이트 공개 주소, 끝 `/` 없음 (웹인증 필수) |
| WEB_SECRET | state 서명용 랜덤 문자열 (웹인증 필수) |
| WEB_PORT | 웹 포트 (기본 8000, Render는 PORT 자동) |
| ALLOWED_GUILDS | 웹인증 허용 서버ID (쉼표 구분, 비우면 GUILD_ID). 슬래시명령과는 무관 (전역 동기화) |
| VERIFIED_ROLE_NAME | 기본 인증 롤 (패널에서 역할 미지정 시) |
| UNVERIFIED_ROLE_NAME | 입장시 부여 미인증 롤 (비우면 끄기) |
| AUTH_CHANNEL_ID / LOG_CHANNEL_ID | 환영멘션 / 입장로그 채널 (선택) |
| MIN_ACCOUNT_AGE_DAYS | 최근생성계정 경고 기준일 (기본 7) |
| GUILD_ID | ALLOWED_GUILDS가 없을 때의 웹인증 기본 허용 서버 |

## 7. 트러블슈팅
- "서버에 입장하지 않았습니다" → 유저가 서버에 먼저 들어와 있어야 역할 지급 가능
- 이메일 미인증 실패 → 디스코드 설정 → 계정에서 이메일 인증 필요
- 중복IP 실패 → 같은 접속기록으로 이미 다른 계정이 인증됨 (관리자가 DB 확인 후 판단)
- 슬래시 명령 안 보임 → 전역 동기화라 **최대 1시간** 걸림. 기다려도 안 뜨면 봇 재시작
