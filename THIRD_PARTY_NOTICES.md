# 외부 기술·자산 및 AI 활용

## 폰트

Poppins와 SUIT는 SIL Open Font License 1.1에 따라 사용하며, 저작권·이용 조건을 포함한 원본 고지를 동봉합니다. 빌드 결과의 `dist/licenses/`에도 복사합니다.

- [Poppins 라이선스](code/frontend/assets/fonts/Poppins-OFL.txt)
- [SUIT 라이선스](code/frontend/assets/fonts/SUIT-OFL.txt)

## 의존성

Python·FastAPI·Pydantic·Uvicorn·Shapely 등은 `requirements.lock.txt`, React·React DOM·TypeScript·Vite 등은 `code/frontend/package-lock.json`에 버전을 기록합니다. 각 패키지의 LICENSE/NOTICE를 따릅니다.

## AI 활용·데이터

- OpenAI·Gemini API: 업무 판단·계획·규정 답변. 외부 모델을 사용하며 자체 학습 모델은 아닙니다.
- Codex/ChatGPT: 기획·코드 작성/수정·검토·시험 도구·문서 작성 지원.
- 지도·계정·차량·관측·장치·운영 매뉴얼: 프로젝트의 합성 시연 자료. 평가 정답은 `tests/expected`에 분리합니다.
