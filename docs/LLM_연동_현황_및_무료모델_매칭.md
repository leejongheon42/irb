# LLM(Claude API) 연동 현황 및 OpenRouter 무료 모델 매칭

작성일: 2026-11-15
기준일 2026-11-15. 코드와 OpenRouter 공개 모델 목록을 대조했다. 구현과 검증 결과는 [OpenRouter 연동 작업 완료](OpenRouter_연동_작업완료.md)에 기록했다. [OpenRouter 공개 모델 API](https://openrouter.ai/api/v1/models)

---

## 1. 현재 상태

- `.env.example` 기본값: `IRB_AI_REVIEW_ENABLED=false`, `ANTHROPIC_API_KEY` 주석 처리 → `ai_enabled()`가 False.
- 모든 AI 호출은 `irb_automation/ai/client.py`의 `create_message`를 거친다.
- OpenRouter는 `OPENROUTER_API_KEY`가 필요하다. 키가 없거나 호출에 실패하면 각 기능의 기존 대체 경로를 사용한다.
- LLM과 무관하게 정상 동작: 개인정보 탐지·비식별화(`deid`), 규칙 엔진, 워크플로, 서류 양식.

## 2. LLM이 필요한 기능 (9개)

| # | 기능 | 위치 | LLM 미연결 시 |
|---|---|---|---|
| 1 | AI 사전검토 보조(논리 불일치·누락 지적) | `engine/ai_review.py`, `web/routes_protocols.py:390` | 사용 불가, 규칙 엔진 결과만 표시 |
| 2 | 서류 점검(QC) AI 심층 분석 | `qc/ai_phase.py` | 체크박스 비활성, 규칙 점검만 수행 |
| 3 | 서류 패키지 필드 초안 자동 작성 | `package/drafting.py` | 규칙 기반으로만 동작 |
| 4 | 보완요청 답변서 초안 | `package/revision.py` | AI 초안 없음, 수동 작성 |
| 5 | 문서 OCR(스캔 PDF·이미지) | `docproc/text.py`, `pipeline/service.py:284` | 사용 불가, 로컬 OCR 대안 없음 |
| 6 | 서류 종류 AI 분류(규칙 신뢰도 0.6 미만 시) | `docproc/classify.py:127` | 규칙 기반 분류 사용 |
| 7 | 계획서 항목 AI 추출 | `docproc/extract_fields.py:638` | 규칙 기반 추출만 수행 |
| 8 | 문서 검토 AI 요약 | `pipeline/service.py:576` | 단계 건너뜀 |
| 9 | 지식 질의응답 AI 답변 | `knowledge/service.py:300` | 검색 발췌 위주의 추출형 답변 |

영향 범위
- 완전히 못 쓰는 기능: #1, #5, #8
- 품질이 낮아지는 기능: #2, #3, #4, #6, #7, #9

## 3. 공급자 활성화

1. `.env`에서 `IRB_AI_REVIEW_ENABLED=true`(또는 `IRB_AI_ENABLED=true`)
2. `IRB_AI_PROVIDER=anthropic`이면 `ANTHROPIC_API_KEY`를 설정한다. `IRB_AI_PROVIDER=openrouter`이면 `OPENROUTER_API_KEY`를 설정한다.
3. `pip install -r requirements-ai.txt`. 로컬에서는 `uvicorn irb_automation.main:app --env-file .env`로 `.env`를 읽어 실행한다.

기관 정보보호 부서의 외부 전송 승인이 필요하다(코드 주석). 전송 전 `privacy.mask_text`로 마스킹한다.
저렴하게 쓰려면 `IRB_AI_MODEL=claude-haiku-5-5`로 바꾸면 코드 수정 없이 가능하다.

## 4. 대체 LLM 선택지

| 선택지 | 비용 | 비고 |
|---|---|---|
| Claude Haiku | 유료(저렴) | 코드 수정 없음, OCR·스키마·인용 모두 지원 |
| Google Gemini API | 무료 한도 | PDF·이미지·JSON 스키마 지원, 어댑터 필요 |
| DeepSeek·Groq 등 OpenAI 호환 | 저렴/무료 한도 | 모델별로 이미지(OCR) 지원 여부 상이 |
| OpenRouter | 일부 무료 모델 | 모델 변동 잦음, 어댑터 필요 |
| 로컬(Ollama·vLLM) | 무료 | 망분리에 적합, GPU 필요·품질 낮음 |

`client.py`는 설정에 따라 Anthropic SDK 또는 OpenRouter Chat Completions를 호출한다.

## 5. OpenRouter 무료 모델 (조회 시점 기준)

`lyria-3` 2종은 음악 생성용이라 제외.

| 모델 | 컨텍스트 | 입력 | 구조화 출력 |
|---|---|---|---|
| inclusionai/ling-3.2-flash:free | 1M | 텍스트·이미지 | 지원 |
| baidu/ernie-5.5-flash-preview:free | 1M | 텍스트 | 지원 |
| nvidia/nemotron-3.5-super-vl:free | 1M | 텍스트·이미지·영상 | 지원 |
| particle-ai/particle-1:free | 1M | 텍스트·이미지 | 지원 |
| dots-studio/dots-3-note-preview:free | 512K | 텍스트·이미지 | 지원 |
| google/gemma-4-31b-it:free | 262K | 텍스트·이미지·영상 | response_format만 |
| google/gemma-4-26b-a4b-it:free | 262K | 텍스트·이미지·영상 | response_format만 |
| nvidia/nemotron-3-ultra-550b-a55b:free | 1M | 텍스트 | 미지원 |
| liquid/lfm-3-1.7b:free | 262K | 텍스트 | 지원(소형) |
| openrouter/free | 200K | 텍스트·이미지 | 지원(자동 라우터) |
| 기타(nemotron-3.5-lightning, nemotron-3-nano-omni, inkling-2-small, flint-v1.0, north-mini-code) | - | - | 제한적 |

## 6. 기능별 모델 매칭

매칭 기준은 이미지 입력, 구조화 출력, 컨텍스트 길이와 합성 샘플 실호출 결과다. 운영 설정의 원본은 `irb_automation/ai/openrouter_models.yaml`이다.

| # | 기능 | 필요 조건 | 1순위 | 대안 |
|---|---|---|---|---|
| 1 | AI 사전검토 보조 | 추론, 구조화 출력, 긴 문서 | nvidia/nemotron-3.5-super-vl:free | baidu/ernie-5.5-flash-preview:free |
| 2 | QC AI 심층 분석 | 구조화 출력, 다수 서류 | nvidia/nemotron-3.5-super-vl:free | inclusionai/ling-3.2-flash:free |
| 3 | 필드 초안 작성 | 한국어 문장 생성, 구조화 출력 | inclusionai/ling-3.2-flash:free | baidu/ernie-5.5-flash-preview:free |
| 4 | 보완요청 답변서 | 한국어 문장 생성, 구조화 출력 | baidu/ernie-5.5-flash-preview:free | inclusionai/ling-3.2-flash:free |
| 5 | 문서 OCR | 이미지 입력 필수 | dots-studio/dots-3-note-preview:free | inclusionai/ling-3.2-flash:free |
| 6 | 서류 분류 | 빠름, 구조화 출력 | inclusionai/ling-3.2-flash:free | liquid/lfm-3-1.7b:free |
| 7 | 항목 추출 | 구조화 출력, 정확도 | baidu/ernie-5.5-flash-preview:free | inclusionai/ling-3.2-flash:free |
| 8 | 검토 요약 | 긴 문서, 구조화 출력 | nvidia/nemotron-3.5-super-vl:free | particle-ai/particle-1:free |
| 9 | 지식 질의응답 | 한국어 답변 | google/gemma-4-26b-a4b-it:free | google/gemma-4-31b-it:free |

공통 최종 대체는 `openrouter/free`다. API의 `models` 배열이 최대 3개이므로 명시 모델은 기능당 최대 2개다.

기능 #1~#4, #6~#8은 JSON 스키마를 사용한다. 명시 모델에는 모델 API 기준 `structured_outputs` 지원 모델을 배정했다. OCR은 이미지 입력을, 질의응답은 한국어 답변과 근거 번호 생성을 사용한다.

## 7. 주의사항

- **OCR(#5)** OpenRouter는 스캔 PDF를 쪽별 PNG로 렌더링하고 3쪽씩 전송한다. 기본 상한은 20쪽이다.
- **인용(#9)** OpenRouter는 답변의 `[n]`을 기존 검색 근거에 연결한다. 유효한 번호가 없으면 검색 결과를 발췌해 답한다.
- **무료 한도** 무료 변형은 분당 20회, 기본 일 50회다. OpenRouter는 크레딧 구매 요건을 충족하는 계정에 일 1,000회를 안내한다. [OpenRouter 무료 모델 안내](https://openrouter.ai/collections/free-models/)
- **개인정보**: 무료 모델 제공자는 입력을 로깅하거나 학습에 쓸 수 있다. 더미 데이터로만 사용하고, 실제 환자·연구 문서에는 유료 API나 로컬 모델을 쓴다.

