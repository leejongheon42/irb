# OpenRouter 무료 LLM 연동 작업 완료

작업일 2026-11-15

## 구현 내용

- `IRB_AI_PROVIDER=openrouter`를 선택하면 공통 AI 클라이언트가 OpenRouter Chat Completions를 호출한다. 앱의 기본 공급자는 Anthropic이다. 기존 AI 기능 9개는 공통 클라이언트를 통해 공급자를 선택한다.
- `irb_automation/ai/openrouter_models.yaml`에 기능별 우선 모델, 대체 모델, 출력 토큰 상한을 설정했다. OpenRouter API의 `models` 최대 길이 3에 맞춰 명시한 무료 모델 1~2개와 `openrouter/free`를 사용한다. [OpenRouter 모델 목록](https://openrouter.ai/models?q=free) · [모델 대체 동작](https://openrouter.ai/docs/guides/routing/model-fallbacks)
- 사전검토, QC, 분류, 추출, 요약과 서류 초안·보완 답변까지 7개 기능의 JSON 스키마를 확인했다. 응답이 스키마에 맞지 않으면 `json_object`로 한 번 더 요청하고, 코드펜스를 제거한 뒤 검사한다. 한도 응답에 `Retry-After`가 60초 이하로 주어지면 한 번 재시도한다. [구조화 출력](https://openrouter.ai/docs/guides/features/structured-outputs) · [API 오류와 재시도](https://openrouter.ai/docs/api/reference/errors-and-debugging)
- 스캔 PDF는 텍스트층이 부족할 때 페이지를 PNG로 변환해 3쪽씩 전송한다. 기본 상한은 20쪽이다. 상한을 넘거나 중간 묶음 OCR이 실패하면 경고를 기록하고 기존 PDF 텍스트층 결과를 사용한다.
- 지식 질의응답은 OpenRouter 답변의 `[n]`을 기존 검색 결과의 출처와 연결한다. 유효한 번호가 없으면 검색 근거를 발췌해 답한다.
- `/admin/ai`에서 공급자, 기능별 모델과 출력 상한, 실제 호출에 사용된 모델 및 더미 데이터 전용 안내를 볼 수 있다.

## 사용 방법

프로젝트의 로컬 `.env`에 공급자와 API 키를 설정했다. 후속 요청에 따라 로컬 설정에서 `IRB_AI_ENABLED=true`로 활성화했다. 저장소의 `.env.example`에는 설정 항목만 포함된다. 채팅에 공유한 키는 검증 후 OpenRouter에서 교체하고, 새 키는 로컬 `.env`에 직접 넣는다.

합성 데이터만 있는 시연 환경에서 다음 설정으로 AI 기능을 켠다. OpenRouter 무료 공급자는 요청을 보관하거나 학습에 사용할 수 있다. 앱에서 OCR을 실행하면 스캔의 원본 이미지도 전송된다. [OpenRouter 데이터 수집 정책](https://openrouter.ai/docs/guides/privacy/data-collection)

```dotenv
IRB_AI_PROVIDER=openrouter
IRB_AI_ENABLED=true
OPENROUTER_API_KEY=<새 키>
IRB_AI_OCR_MAX_PAGES=20
```

```powershell
python -m pip install -r requirements.txt
python -m uvicorn irb_automation.main:app --env-file .env
```

Docker Compose는 `.env`를 읽는다. Docker로 실행하는 경우 새 의존성을 포함하도록 이미지를 다시 빌드한다.

## 검증

- 최종 `pytest -q` 240개 통과. 변경 Python 파일 Ruff 검사 통과. 빌드한 wheel에 `openrouter_models.yaml`이 포함된 것도 확인했다.
- 로컬 데모 서버의 관리자 화면에서 공급자, 모델 9행, 더미 데이터 전용 문구를 확인했다. 데스크톱과 모바일에서 페이지 전체의 가로 넘침이 없었고 모바일에서는 키보드로 모델 표를 가로 스크롤할 수 있었다.
- 합성 입력으로 OpenRouter 실호출을 검증했다. 사전검토, QC, 서류 초안, 보완 답변, 분류, 항목 추출, 요약, 지식 질의응답이 각각 결과를 반환했다. 항목 추출에서 표본 수 120건이 일치했고, 질의응답의 근거 번호가 합성 검색 결과에 연결됐다.
- OCR 최초 요청의 HTTP 400은 모델 목록이 4개였기 때문이다. 후보 수를 3개로 줄이고 이미지 모델을 비교했다. Dots 3 Note와 Ling 3.2는 `합성자료 120건`을 정확히 전사했다. Nemotron 3.5 Super VL은 첫 글자를 틀려 OCR 대안에서 제외했다. 최종 모델 순서로 다시 실행한 OCR은 대체 모델인 Ling 3.2에서 문구가 정확히 나왔다.

합성 입력 몇 건으로 연결과 응답 형식을 확인했다. 실제 IRB 문서의 정확도와 무료 모델 공급자의 가용성은 검증 범위를 벗어난다. 무료 변형은 분당 20회, 기본 일 50회이며, 크레딧 구매 요건을 충족한 계정은 일 1,000회까지 사용할 수 있다. [OpenRouter 무료 모델 한도](https://openrouter.ai/collections/free-models/)

합성 스모크 테스트를 다시 실행하려면 저장소 루트에서 다음 명령을 사용한다. 뒤에 `ocr`, `extract` 등 기능 키를 붙이면 해당 기능만 실행한다.

```powershell
python -X utf8 -m scripts.openrouter_smoke
python -X utf8 -m scripts.openrouter_smoke ocr
```
