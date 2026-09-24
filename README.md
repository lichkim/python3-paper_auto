# python3-paper_auto

관심 주제의 최신 논문을 arXiv·Semantic Scholar·APS·Nature에서 수집하고, 중복 통합과
점수화를 거쳐 Telegram 및 이메일로 전달하는 독립 실행 프로젝트입니다. `main.py` 하나로
일일 스캔, 주간 리포트, 검색, PDF 다운로드와 한국어 LaTeX 번역을 실행할 수 있습니다.

이 배포본에는 원본 운영 DB, 사용자 정보, 로그, CSV와 API 키가 들어 있지 않습니다.

## Docker로 실행 (로컬 Python 설치 불필요)

이 프로젝트 폴더에서 아래 명령을 실행합니다. Docker와 Docker Compose가 필요하며,
Python 3.11과 라이브러리는 이미지 안에 설치됩니다.

```bash
# 최초 한 번만 설정 파일 생성 (기존 .env는 유지)
test -f .env || cp .env.example .env
chmod 600 .env
docker compose up -d --build
docker compose exec paper-auto python main.py doctor
```

컨테이너 이름은 `paper-auto`이며, 작업 폴더는 `/app`입니다. 컨테이너는 명령을
기다리며 계속 실행됩니다. 논문 수집이나 정기 실행 예약은 별도로 실행해야 합니다.

```bash
# 컨테이너에 접속 (exit으로 나와도 컨테이너는 계속 실행)
docker exec -it paper-auto bash

# 접속 없이 호스트 터미널에서 논문 수집 및 순위 확인
docker exec paper-auto python main.py daily --skip-telegram
docker exec paper-auto python main.py top --limit 10

# 상태 확인, 보관한 채 중지, 기존 컨테이너 다시 시작
docker compose ps
docker compose stop
docker compose start
```

프로젝트 폴더 전체를 `/app`에 연결하므로 `.env`, `topics.json` 수정은 다음 명령부터
반영되고, `data/`, `reports/`, `downloads/`, `translations/` 결과는 호스트에도 남습니다.
프로젝트 폴더는 현재 위치에 유지하세요. `.env`와 수집 결과는 이미지에 포함하지 않습니다.

컨테이너를 유지하려면 `docker compose down` 또는 `docker rm`으로 삭제하지 마세요.
평소 재접속은 `docker exec`, 중지 후 재개는 `docker compose start`를 사용합니다.
`up --build`는 최초 생성이나 의존성 업데이트 때만 사용합니다. 이미지 또는 설정이
바뀌면 컨테이너를 재생성할 수 있습니다.

`restart: unless-stopped`를 설정했으므로 수동으로 중지하지 않았다면 Docker 데몬이
다시 시작될 때 컨테이너도 다시 실행됩니다. Docker 자체의 부팅 시 시작 여부는
호스트 설정에 따릅니다.
([Docker Compose 문서](https://docs.docker.com/reference/compose-file/services/))

기본 실행 UID/GID는 현재 작업 사용자와 같은 `1000:1000`입니다. 다른 사용자는
`.env`에 `PAPER_AUTO_UID`와 `PAPER_AUTO_GID`를 실제 `id -u`, `id -g` 값으로 설정한 뒤
최초 생성하세요. 라이브러리를 변경하면 이미지도 다시 빌드해야 합니다.
기본 이미지에는 LaTeX 컴파일러가 없으므로 번역 기능은 `.env`에서
`PAPER_TREND_COMPILE_TEX=false`로 설정하여 `.tex`까지만 생성합니다.

## 1. 로컬 설치 (Docker를 사용하면 생략)

Python 3.11 이상을 권장합니다.

```bash
cd /home/chanpyo/Desktop/archive/Python3/python3-paper_auto
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cp .env.example .env
chmod 600 .env
```

이후 모든 명령은 프로젝트 폴더에서, 가상환경을 활성화한 상태로 실행합니다.

## 2. 비밀키 설정

실제 키와 계정 정보는 `.env`에만 입력합니다. `.env`는 Git에서 제외되며 배포 ZIP에도
포함되지 않습니다.

```dotenv
# 선택 사항: 없어도 arXiv와 지정 저널 검색은 가능
SEMANTIC_SCHOLAR_API_KEY=
DEEPSEEK_API_KEY=

# Telegram 전송과 봇 사용 시 필요
TELEGRAM_BOT_TOKEN=
TELEGRAM_BOT_USERNAME=내봇사용자이름
TELEGRAM_CHAT_ID=내숫자ChatID

# 이메일 전송 시 필요
PAPER_TREND_SMTP_USER=your-address@gmail.com
PAPER_TREND_SMTP_PASSWORD=Gmail앱비밀번호
PAPER_TREND_EMAIL_TO=first@example.com,second@example.com

# arXiv 요청 식별용 연락처: 실제 관리 이메일로 변경 권장
PAPER_TREND_USER_AGENT=PaperTrend/1.0 (mailto:your-address@example.com)
```

- arXiv는 API 키 없이 동작합니다.
- Semantic Scholar 키가 없으면 낮은 호출 한도로 동작합니다.
- DeepSeek 키가 없으면 수집은 가능하지만 한 줄 요약과 번역은 생략됩니다.
- Telegram은 BotFather에서 받은 토큰과 숫자 Chat ID가 모두 필요합니다.
- Gmail은 계정 비밀번호가 아니라 Google의 앱 비밀번호를 사용합니다.
- 지원되는 모든 비밀값은 `이름_FILE=/run/secrets/...` 형태로 파일에서 읽을 수도 있습니다.
- `.env`를 GitHub, 메신저, 이메일 또는 공유 ZIP에 넣지 마세요.

설정을 확인합니다.

```bash
python main.py doctor
```

`doctor`는 파일과 설정 여부를 확인합니다. 실제 외부 검색까지 확인하려면 다음의 dry-run을
사용합니다. 이 명령은 DB·CSV·리포트를 저장하지 않고 Telegram도 보내지 않습니다.

```bash
python main.py --dry-run
```

## 3. 추적 주제와 저널 설정

[`topics.json`](topics.json)을 수정합니다. `name`은 화면에 보일 이름이고, `query`는 논문
검색에 사용할 영문 검색어입니다.

```json
{
  "topics": [
    {
      "name": "양자 오류 정정",
      "query": "quantum error correction",
      "journal_match_ratio": 1.0,
      "enabled": true,
      "sources": ["arxiv", "semantic_scholar", "aps", "nature"]
    }
  ]
}
```

사용 가능한 source는 `arxiv`, `semantic_scholar`, `aps`, `nature`, `crossref`입니다.
`enabled`를 `false`로 바꾸면 해당 주제를 잠시 중지합니다. 지정 저널은
[`journals.json`](journals.json)에서 관리하며 기본값은 다음 네 곳입니다.

- Physical Review X
- PRX Quantum
- Nature Physics
- npj Quantum Information

## 4. 기본 실행법

```bash
python main.py                         # daily와 동일: 일일 수집·저장·Telegram 전송
python main.py daily --skip-telegram   # 수집·저장하되 Telegram만 생략
python main.py --dry-run               # 아무것도 저장/전송하지 않고 결과 확인
python main.py weekly                  # 최근 7일 주간 HTML 리포트와 Telegram
python main.py top --limit 10          # DB에 저장된 논문 순위
python main.py search quantum error correction
```

검색 후 PDF와 번역본이 필요할 때만 선택적으로 실행합니다. Telegram에는 PDF나 번역본을
자동 첨부하지 않습니다.

```bash
python main.py top --limit 10
python main.py fetch 1                 # 1위 논문의 공개 PDF 다운로드
python main.py fetch 1 --translate     # 다운로드 후 한국어 LaTeX 번역
python main.py translate ./paper.pdf   # 이미 가진 PDF 번역
```

LaTeX를 PDF로도 컴파일하려면 `latexmk`가 필요합니다. 설치하지 않았다면 `.env`에서
`PAPER_TREND_COMPILE_TEX=false`로 설정하면 `.tex`까지만 생성합니다.

전체 옵션은 다음 명령으로 확인할 수 있습니다.

```bash
python main.py --help
python main.py daily --help
```

## 5. Telegram 설정과 명령 봇

먼저 연결 테스트를 보냅니다.

```bash
python main.py telegram-test
```

사용자의 Telegram 명령을 계속 받으려면 별도 프로세스로 봇을 실행해 둡니다.

```bash
python main.py bot
```

주요 Telegram 명령은 다음과 같습니다.

```text
/topics                                  내 주제 목록
/topic_add 주제명 | 영문 검색어          주제 추가
/topic_remove 번호                       주제 제거
/search 검색어                           관련 논문과 강의노트 즉시 검색
/lecture 검색어                          강의노트·튜토리얼 검색
/papers_csv                              내가 받은 누적 논문 CSV
/whoami                                  현재 Chat ID
/help                                    전체 사용법
```

관리자만 `/invite`로 12시간 유효한 일회용 초대 링크를 만들 수 있습니다. 사용자는 각자
독립된 주제를 가지며, `/users`와 `/revoke 번호`는 관리자 명령입니다. 처음 실행할 때
Telegram의 `/` 명령 힌트도 자동 등록됩니다.

## 6. 이메일과 Semantic Scholar 야간 캐시

```bash
python main.py email-test                         # SMTP 연결 시험
python main.py email-test --to user@example.com
python main.py email-papers                       # 익명 주제별 APS·Nature 논문 발송
python main.py email-papers --to a@example.com,b@example.com
python main.py semantic-prefetch                  # 다음날 사용할 Semantic Scholar CSV 생성
```

`email-papers`는 사용자의 이름과 Chat ID를 노출하지 않고 주제와 지정 저널 논문만
보냅니다. arXiv와 Semantic Scholar 항목은 이 이메일에서 제외됩니다.

## 7. 자동 실행 예시(cron)

아래 예시는 월~금 오전 9시 일일 Telegram, 토요일 오전 9시 주간 리포트, 월~토 오전
9시 10분 이메일, 다음 실행을 위한 밤 10시 5분 Semantic Scholar 사전 수집입니다.
`/절대/경로`와 Python 경로를 실제 설치 위치로 바꾸세요.

```cron
CRON_TZ=Asia/Seoul
0 9 * * 1-5 cd /절대/경로/python3-paper_auto && .venv/bin/python main.py daily >> cron.log 2>&1
0 9 * * 6 cd /절대/경로/python3-paper_auto && .venv/bin/python main.py weekly >> weekly-cron.log 2>&1
10 9 * * 1-6 cd /절대/경로/python3-paper_auto && .venv/bin/python main.py email-papers >> email-cron.log 2>&1
5 22 * * 0-5 cd /절대/경로/python3-paper_auto && .venv/bin/python main.py semantic-prefetch >> semantic-cron.log 2>&1
```

`python main.py bot`은 계속 실행되는 프로세스이므로 cron보다 systemd, Docker 또는
프로세스 관리 도구로 상시 실행하는 편이 안전합니다. 같은 Telegram 토큰으로 bot
프로세스를 두 개 동시에 실행하지 마세요.

## 8. 생성되는 데이터

```text
data/paper_trend.db                 SQLite 메타데이터·사용자별 주제·전송 이력
data/papers.csv                     중복 제거된 누적 논문과 한 줄 요약
data/semantic_scholar_prefetch.csv  다음날 사용할 야간 캐시
data/exports/                       사용자별 개별_논문.csv
reports/                            일일 Markdown·주간 HTML 리포트
downloads/                          사용자가 선택한 원문 PDF
translations/                       한국어 LaTeX와 컴파일된 PDF
```

논문 중복 판정은 DOI, 버전을 제거한 arXiv ID, 정규화된 제목 순으로 수행합니다. 따라서
같은 논문이 arXiv와 저널 양쪽에서 발견되거나 arXiv 새 버전이 등록돼도 한 논문으로
통합됩니다.

## 9. 문제 해결

- `python main.py doctor`에서 `[FAIL]`이 나오면 해당 JSON 또는 설정값을 먼저 고칩니다.
- Telegram 무응답이면 `python main.py bot`이 실행 중인지, 토큰과 Chat ID가 맞는지
  확인합니다.
- arXiv 확인은 `python main.py --dry-run` 결과의 `arxiv` 수집 건수와 경고를 봅니다.
- Semantic Scholar가 429를 반환하면 밤 10시 사전 수집을 사용하고 호출 간격을 줄이지
  마세요.
- 중복 실행을 피하려면 같은 시각에 daily 또는 bot 프로세스를 두 개 띄우지 않습니다.
- 자세한 로그가 필요하면 `python main.py --verbose daily --dry-run`을 사용합니다.

## 10. 테스트

```bash
python -m unittest discover -s tests -v
```

현재 배포본의 자동 테스트는 외부 전송을 모의 처리하므로 실제 Telegram이나 이메일을
보내지 않습니다.
