# 외부 기술·자산 및 AI 활용 고지

## 동봉된 폰트

`code/frontend/assets/fonts/`의 Poppins와 SUIT는 해당 폴더의 원본 SIL Open Font License 고지와 함께 배포합니다. 원본 저작권·예약 폰트명·이용 조건은 각 라이선스 파일을 따릅니다. 프론트 빌드의 `dist/licenses/`에도 두 고지를 복사합니다.

- Poppins: `code/frontend/assets/fonts/Poppins-OFL.txt`
- SUIT: `code/frontend/assets/fonts/SUIT-OFL.txt`

## 실행·개발 의존성

Python·FastAPI·Pydantic·Uvicorn·Shapely 등은 `requirements.lock.txt`, React·React DOM·TypeScript·Vite 등은 `code/frontend/package-lock.json`의 버전을 사용합니다. 설치된 의존성 본체는 이 저장소에 포함하지 않습니다. 각 패키지의 LICENSE/NOTICE가 해당 패키지에 적용되며, 프로젝트 코드와 외부 패키지의 권리를 동일하게 취급하지 않습니다.

## 외부 모델과 AI 개발 도구

GPT-6 Luna·Gemini 3.8 Flash API를 업무 판단·규정 답변에 사용한 경로와 모의 경로를 구분합니다. 모델 본체·제공자 API 키는 배포하지 않습니다. 모델 자체 학습·개발의 성과로 주장하지 않습니다.

프로젝트 기획·코드 작성/수정·검토·시험 도구·문서 준비에 Codex/ChatGPT의 도움을 활용했습니다. 팀이 구현·통합한 부분은 관측 계약, 합성 환경, 공간 분석, 관계/권한, 도구와 상태 흐름, 후속 확인, 화면 연결입니다.

## 데이터·공개 범위

지도·계정·차량·관측·장치와 운영 매뉴얼은 합성 시연 자료입니다. 평가 정답은 `tests/expected`에 분리되어 있습니다.

