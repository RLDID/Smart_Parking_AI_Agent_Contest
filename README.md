# PARKLINE · 무인 스마트 주차장 AI 관리자

제4회 경남 AI·SW 경진대회 **사회문제 해결형 AI Agent** 프로젝트입니다. 가상 주차장의 상황을 관측하고, 운영 기준에 따라 필요한 행동을 실행한 뒤 결과를 확인하는 AI 관리자를 구현했습니다.

## 실행 방법

### 1. 준비

Windows, Python 3.12, Node.js 24 계열과 npm을 사용합니다. 의존성 설치에는 인터넷이 필요하며, API 키 없이도 모의 판단으로 실행할 수 있습니다.

### 2. 백엔드 실행

저장소 루트에서 PowerShell을 열고 실행합니다. 기존 `.venv`가 있으면 첫 줄은 생략합니다.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.venv\Scripts\python.exe code/frontend/scripts/run-integration-backend.py
```

### 3. 프론트 실행

별도 PowerShell을 저장소 루트에서 열고 실행합니다.

```powershell
cd code/frontend
npm ci
npm run dev:integration
```

브라우저에서 `http://127.0.0.1:8000/?mode=live`에 접속합니다. 프론트는 8000, 백엔드는 8010 포트를 사용합니다. `mode=live`는 백엔드 연결 화면을 뜻하며 유료 AI를 자동 호출하지 않습니다.

### 4. 로그인과 시연 시작

| 데모 계정 | 역할 |
|---|---|
| `demo-operator` | 시험 회차·시나리오 제어 |
| `demo-owner` | 시설 소유자 |
| `demo-driver` / `demo-driver-2` | 차량 차주 |

공통 비밀번호는 `parking-demo-only`입니다. 운영자와 차주를 동시에 사용하려면 브라우저 프로필을 분리합니다.

1. `demo-operator`로 로그인해 `로컬 시험 제어`에서 상황과 새 시험 회차를 선택합니다.
2. `http://127.0.0.1:8000/devtools`에서 관측 시간을 진행합니다.
3. `http://127.0.0.1:8000/devtools/operations`에서 모의 업무 Agent를 시작하거나 처리합니다. 로그인·회차 생성만으로 Agent가 시작되지는 않습니다.

### 5. 실제 AI 사용 시 API 키 등록

사용할 제공자에서 API 키를 발급받아 Windows의 **환경 변수 편집 → 사용자 변수 → 새로 만들기**에 등록합니다.

| 발급처 | 변수 이름 | 변수 값 |
|---|---|---|
| [OpenAI](https://platform.openai.com/api-keys) | `OPENAI_API_KEY` | 발급받은 비밀키 |
| [Google AI Studio](https://aistudio.google.com/apikey) | `GEMINI_API_KEY` | 발급받은 비밀키 |

등록 후 새 터미널을 열고, 기존 백엔드를 종료한 뒤 아래 명령으로 실행합니다. 실제 호출에는 제공자의 결제·모델 사용 권한이 필요합니다. 키는 소스나 Git에 넣지 않습니다. `.env` 파일은 자동으로 읽지 않습니다.

```powershell
.venv\Scripts\python.exe code/frontend/scripts/run-integration-backend.py --live-config data/samples/live-read-defaults.json
```

화면의 AI 조회에서 제공자를 선택해 요청합니다. 자동 업무의 실제 모델 사용은 개발 콘솔에서 `live` 모드를 별도로 선택합니다.

### 6. 종료와 재실행

각 서버 터미널에서 `Ctrl+C`로 종료합니다. 데이터는 `data/local/contest.sqlite3`에 저장됩니다. 같은 명령으로 다시 실행하고, 복구된 회차는 운영자가 확인한 뒤 진행합니다.
