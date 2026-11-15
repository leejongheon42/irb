# MVP 작업 계획: 서류 업로드 → AI 심사·채점 → 점수표 확인

작성일: 2026-11-15 · 대상 브랜치: `mvp`

## 1. 목표

기존 서비스의 서류 품질 점검(QC) 엔진을 그대로 쓰되, 화면은 3단계 흐름만 남겨 테스트할 수 있게 한다.

```
① 서류 업로드  ──[AI 서류 심사]──▶  ② 검토·카테고리별 채점  ──▶  ③ 채점 점수표·점수 기준
   /mvp                               /mvp/review/{id}              /mvp/review/{id}/scores
```

범위 밖(이번 MVP에서 하지 않음): 과제 등록, 이슈 검토·확정, 자동 교정, 재점검, 서식 자료실, 지식 검색, 관리자 화면.
이 기능들의 코드와 경로는 지우지 않고 화면에서만 숨긴다.

## 2. 현재 코드에서 재사용하는 것

| 필요한 것 | 이미 있는 것 | 위치 |
|---|---|---|
| 파일 검증(확장자·크기·시그니처) | `validate_upload`, `ALLOWED_EXT` | `irb_automation/submission/validate.py` |
| 텍스트 추출(PDF·DOCX·HWP·HWPX·XLSX·이미지 OCR 등) | `extract_text`, `detect_format` | `irb_automation/docproc/text.py` |
| 서류 종류 자동 추정 | `guess_doc_type` | `irb_automation/submission/guess.py` |
| 점검 실행(규칙 → AI 심층 → 오탈자·숫자) | `qc.service.run_uploads` | `irb_automation/qc/service.py:181` |
| 점검 결과 저장 | `QCRun`(score, verdict, issues, docs, phases) | `irb_automation/qc/models.py` |
| 감점 기준·카테고리 10종 | `scoring`, `categories` | `irb_automation/qc/criteria.yaml` |
| 드래그앤드롭 업로드 스크립트 | `.dropzone` 처리 | `irb_automation/web/static/submission.js` |
| 백그라운드 실행 + 상태 폴링 | `ThreadPoolExecutor`, `after_commit` 디스패치, `/api/pipeline/{id}` 폴링 | `irb_automation/pipeline/service.py:76`, `web/static/pipeline.js` |
| 컬럼 자동 추가 | `init_db`의 `ALTER TABLE` 보충 | `irb_automation/database.py:96` |

## 3. 새로 만들어야 하는 것 (현재 없는 부분)

1. **사이드바 없는 MVP 레이아웃**: `base.html`은 사이드바가 고정으로 들어 있다.
2. **업로드와 심사의 분리**: 지금 `/qc`는 업로드와 점검이 한 번의 POST로 묶여 있다. MVP는 "올려 두고 목록 확인 → 버튼으로 심사 시작"이어야 한다.
3. **비동기 채점**: 지금 `/qc` POST는 점검이 끝날 때까지 응답이 없다. "채점 중" 메시지를 띄우려면 백그라운드 실행과 상태 조회가 필요하다.
4. **카테고리별 점수**: 지금 점수는 총점 하나뿐이다(`qc/scoring.py`의 `assess`: 100 − 확정 이슈 감점). 카테고리별 배점과 소계가 없다.
5. **검토 없는 점수**: 지금 총점은 사람이 "확정"한 이슈만 감점한다. MVP에는 검토 단계가 없으므로 후보 이슈까지 반영한 점수가 필요하다.

## 4. 작업 순서

### 0단계: 브랜치 생성

- 현재 브랜치 `claude/gifted-hawking-yh1og1`에 커밋되지 않은 변경이 많다(수정 53개, 새 파일 다수: `qc/scoring.py`, `qc/bundle.py`, `ai/openrouter.py` 등). MVP는 이 변경에 의존한다.
- 순서: ① 현재 변경을 현재 브랜치에 커밋 → ② `git switch -c mvp` → ③ 이후 작업은 `mvp`에만 커밋.
- 완료 기준: `git branch`에 `mvp`가 있고, `pytest`가 브랜치 생성 직후 기준으로 통과한다.

### 1단계: MVP 모드와 레이아웃 (사이드바 숨김)

| 파일 | 변경 |
|---|---|
| `irb_automation/config.py` | `mvp_mode: bool` 추가(환경 변수 `IRB_MVP_MODE`, `mvp` 브랜치 기본값 켜짐) |
| `irb_automation/web/deps.py` | `render()`가 템플릿에 `mvp_mode` 전달 |
| `irb_automation/web/templates/base.html` | `mvp_mode`면 `<aside class="sidebar">`와 화면 가이드(`?` 버튼)를 출력하지 않음 |
| `irb_automation/web/templates/mvp/_layout.html` (신규) | `base.html` 상속. 상단에 서비스명, 사용자명·로그아웃, 3단계 진행 표시(①업로드 ②심사·채점 ③점수표) |
| `irb_automation/web/static/mvp.css` (신규) | 사이드바 없는 1단 레이아웃, 단계 표시, 드롭존, 점수 카드·표 |
| `irb_automation/web/routes_main.py` | `mvp_mode`면 `/`와 로그인 후 이동을 `/mvp`로 보냄 |

- 사이드바는 템플릿에서 출력 자체를 하지 않는다(CSS로 가리는 방식 아님).
- 기존 경로(`/qc`, `/protocols` 등)는 주소를 직접 치면 열리지만 사이드바 없이 보인다. MVP 테스트에는 지장이 없어 이번에는 막지 않는다.
- 완료 기준: 로그인하면 `/mvp`로 이동하고, 어느 MVP 화면에도 왼쪽 사이드바가 없다.

### 2단계: 서류 업로드 페이지 (`/mvp`)

**데이터**: `irb_automation/mvp/models.py` (신규)

```
MvpUpload: id, user_id, name, path, size, fmt(pdf/docx/hwp…), doc_type(추정값), doc_type_label,
           status(ok | rejected), message(거부·경고 사유), run_id(심사에 쓰인 뒤 연결, 없으면 대기 중), created_at
```

- 파일은 `data/mvp/{user_id}/{uuid}_{안전한 파일명}`에 저장한다.
- `run_id`가 비어 있는 행이 "심사 대기 목록"이다.

**경로**: `irb_automation/web/routes_mvp.py` (신규, `main.py`에 등록)

| 경로 | 동작 |
|---|---|
| `GET /mvp` | 업로드 화면. 드롭존, 대기 목록, 지난 심사 목록(최근 10건) |
| `POST /mvp/files` | 파일 여러 개 저장. `fetch` 요청이면 JSON(행 목록), 일반 폼이면 `/mvp`로 이동 |
| `POST /mvp/files/{id}/delete` | 대기 목록에서 삭제(본인 것만, 파일도 삭제) |
| `POST /mvp/files/clear` | 대기 목록 비우기 |

**화면**: `templates/mvp/upload.html`, `static/mvp.js` (신규)

- 드롭존: 끌어다 놓기 + 클릭해서 선택 + 여러 파일 동시 선택. 놓는 즉시 업로드하고 목록에 추가한다.
- 목록 열: 파일명 · 형식 · 크기 · 추정 서류 종류 · 상태(업로드됨 / 거부 사유) · 삭제.
- **AI 서류 심사** 버튼: 대기 목록에 정상 파일이 1건 이상일 때만 활성화. 누르면 3단계로 이동.
- 빈 상태 문구, 업로드 중 표시, 실패 문구(용량 초과·지원하지 않는 형식)를 넣는다.

**지원 파일 형식**

| 구분 | 형식 | 처리 |
|---|---|---|
| 지금 지원 | PDF, DOCX, DOC, HWP, HWPX, XLSX, XLS, CSV, TXT, JPG, PNG | 그대로 사용 |
| 이번에 추가 | PPTX, RTF, ODT | `ALLOWED_EXT`와 시그니처 표에 추가. 추출은 `extract_text`가 LibreOffice 변환으로 이미 처리 |
| 조건부 | 이미지, 스캔 PDF | AI OCR이 켜져 있어야 본문을 읽음. 못 읽으면 목록과 결과에 "본문을 읽지 못함"으로 표시 |
| 제외 | ZIP | MVP에서는 받지 않음(풀어서 올리도록 안내) |

- 파일당 최대 크기는 기존 `IRB_MAX_UPLOAD_MB`(기본 50MB)를 따른다.
- 완료 기준: 서로 다른 형식 5개를 한 번에 끌어다 놓으면 목록에 5행이 생기고, 새로고침해도 남아 있으며, 삭제가 된다.

### 3단계: 심사·채점 페이지 (`/mvp/review/{id}`)

**비동기 실행**

| 파일 | 변경 |
|---|---|
| `irb_automation/qc/models.py` | `QCRun`에 `status`(queued / running / done / failed, 기본 done), `stage`(진행 단계 문구), `error` 컬럼 추가. 기존 행은 done으로 읽힘 |
| `irb_automation/mvp/service.py` (신규) | `start_review(db, user)`: 대기 목록으로 `QCRun(status="queued")` 생성 → 커밋 후 스레드 풀에 작업 제출. 작업은 자체 DB 세션을 열어 `qc.service` 실행 로직을 돌리고 결과를 같은 `QCRun`에 채움 |
| `irb_automation/qc/service.py` | `run()`이 기존 `QCRun`을 받아 채울 수 있게 선택 인자 추가(`qr=None`), 단계가 바뀔 때 `stage` 갱신 콜백 호출 |

- 실행 방식은 `pipeline/service.py`의 `_pool()` + `after_commit` 패턴을 그대로 따른다. 테스트용 동기 모드(`pipeline_sync`)도 같이 적용한다.
- 작업 중 예외는 `status="failed"`, `error`에 사용자용 문구를 남긴다. 서버가 재시작돼 `running`으로 멈춘 실행은 시작 후 일정 시간(예: 10분)이 지나면 실패로 표시한다.

**경로**

| 경로 | 동작 |
|---|---|
| `POST /mvp/review` | 심사 시작 → `/mvp/review/{id}`로 이동 |
| `GET /mvp/review/{id}` | 진행 중이면 "채점 중" 화면, 끝났으면 점수 화면 |
| `GET /api/mvp/review/{id}` | `{status, stage, score, verdict}` JSON. 화면이 1.5초 간격으로 조회 |

**화면**: `templates/mvp/review.html`

- 채점 중: "AI가 서류를 채점하고 있습니다" + 현재 단계(서류 읽는 중 → 규칙 점검 → AI 심층 분석 → 점수 계산) + 서류 목록. 폴링해서 끝나면 자동으로 점수 화면으로 바뀐다.
- 채점 완료: 총점(100점 만점), 판정, 카테고리별 점수 막대 5개, 서류별 이슈 건수, **채점 점수표 보기** 버튼.
- 실패: 사유와 **다시 심사** 버튼, 업로드 화면으로 돌아가기.
- 본문을 한 글자도 읽지 못한 경우 점수 대신 "채점 불가"와 사유를 보여 준다(지금 엔진은 이 경우 점수를 내지 않는다).
- AI가 꺼져 있거나 호출이 실패하면 규칙 점검만으로 채점하고 "AI 심층 분석 없이 채점됨"을 표시한다.
- 완료 기준: 버튼을 누르면 즉시 "채점 중" 화면이 뜨고, 끝나면 새로고침 없이 점수가 나타난다.

### 4단계: 카테고리별 채점 로직

`irb_automation/qc/category_scores.py` (신규), 기준은 `criteria.yaml`의 `scoring.areas`에 둔다. 배점은 `docs/서류점검_고도화_계획.md` 6장 설계안을 따른다.

| 채점 영역 | 배점 | 포함되는 기존 카테고리 |
|---|---|---|
| 필수 기재사항 | 35 | `required`, `placeholder` |
| 문서 간 일치 | 25 | `cross`, `record` |
| 규제·윤리 준수 | 20 | `regulatory` |
| 형식·버전 | 10 | `version`, `numbers` |
| 표현·가독성 | 10 | `terminology`, `typo`, `context` |

계산 규칙

1. 제외: 다른 이슈에 병합된 이슈, 조언 성격 이슈, 인용이 검증되지 않은 AI 이슈, 점검하지 못한 항목.
2. 같은 원인 묶음(`group`)은 가장 높은 심각도로 1건만 센다.
3. 감점: Critical 15 · Major 7 · Minor 2 (기존 `scoring` 값 그대로).
4. 영역 점수 = `max(0, 배점 − 영역 내 감점 합)`. 영역 밖으로 감점이 넘어가지 않는다.
5. 총점 = 영역 점수 합(100점 만점).
6. 판정: Critical 1건 이상 → 미통과 / 80점 이상 → 통과 / 그 외 → 보완 필요.
7. 검토 단계가 없으므로 확정 이슈와 후보 이슈를 모두 감점한다.

- 출력: 영역별 `{배점, 점수, 감점, 심각도별 건수, 이슈 id 목록}`, 서류별 소계, 총점, 판정.
- 결과는 `QCRun.counts["areas"]`에 저장해 점수 화면과 점수표가 같은 값을 쓴다.
- 기존 `/qc` 화면의 점수(`assess`)는 건드리지 않는다.
- 테스트(`tests/test_mvp.py`): 영역 하한 0, 묶음 중복 감점 없음, Critical 시 미통과, 미검증 AI 이슈 제외, 영역 합 = 총점.

### 5단계: 채점 점수표·점수 기준 페이지 (`/mvp/review/{id}/scores`)

`templates/mvp/scores.html`, 탭 2개.

**탭 1. 채점 점수표**

- 요약 표: 영역 · 배점 · 감점 · 점수 · Critical/Major/Minor 건수, 맨 아래 총점과 판정.
- 영역별 상세: 영역을 펼치면 감점 항목 목록(서류명 · 지적 내용 · 원문 인용 · 심각도 · 감점).
- 서류별 표: 서류명 · 서류 종류 · 이슈 건수 · 감점 소계 · 본문 인식 여부.

**탭 2. 점수 기준**

- 영역별 배점과 각 영역이 무엇을 보는지 설명.
- 심각도별 감점표(Critical 15 / Major 7 / Minor 2)와 심각도 정의.
- 판정 기준(미통과 / 보완 필요 / 통과).
- 서류 종류별 필수 기재 항목 목록(`criteria.yaml`의 `required`에서 항목명·심각도를 그대로 출력).
- 기준 버전(`QCRun.criteria_version`)과 "운영용 기준이며 법령 원문 검수가 끝난 기준은 아님" 안내(`criteria.yaml` 머리말과 같은 내용).

- 화면의 숫자는 모두 `criteria.yaml`에서 읽는다(템플릿에 숫자를 직접 쓰지 않는다).
- 하단 버튼: 새 서류 심사하기(`/mvp`).
- 완료 기준: 점수표의 영역 점수 합이 2단계 화면 총점과 같고, 기준 탭의 감점 값이 `criteria.yaml`과 같다.

### 6단계: 검증

1. `pytest` 전체 통과(기존 테스트가 MVP 모드에서 깨지지 않는지 포함. 필요하면 테스트 설정에서 `IRB_MVP_MODE=0`).
2. `tests/test_mvp.py`: 업로드 → 목록 → 심사 시작 → 상태 조회 → 점수표 응답까지 동기 모드로 통과.
3. 브라우저 확인: 드래그앤드롭, 채점 중 표시와 자동 전환, 점수표·기준 탭, 사이드바 없음.
4. 실패 경로: 지원하지 않는 형식, 용량 초과, 빈 목록에서 심사 시도, 본문 인식 실패 파일, AI 꺼짐.

## 5. 변경 파일 요약

| 구분 | 파일 |
|---|---|
| 신규 | `irb_automation/mvp/__init__.py`, `mvp/models.py`, `mvp/service.py`, `qc/category_scores.py`, `web/routes_mvp.py`, `web/templates/mvp/{_layout,upload,review,scores}.html`, `web/static/mvp.css`, `web/static/mvp.js`, `tests/test_mvp.py` |
| 수정 | `config.py`, `main.py`, `web/deps.py`, `web/routes_main.py`, `web/templates/base.html`, `qc/models.py`, `qc/service.py`, `qc/criteria.yaml`, `submission/validate.py` |

## 6. 결정이 필요한 사항

| # | 질문 | 계획서의 기본값 |
|---|---|---|
| 1 | 채점 영역과 배점 | 5개 영역(35/25/20/10/10). 기존 10개 카테고리를 그대로 10행으로 보여 주는 방식도 가능 |
| 2 | 로그인 | 기존 로그인 유지(데모 계정). 로그인 없이 바로 쓰게 하려면 별도 작업 필요 |
| 3 | 현재 브랜치의 미커밋 변경 | 현재 브랜치에 먼저 커밋한 뒤 `mvp`를 만든다 |
| 4 | 기존 경로 차단 | 사이드바만 숨기고 주소 직접 접근은 막지 않는다 |
| 5 | AI 사용 | 설정에 AI 키가 있으면 항상 AI 심층 분석 포함, 없으면 규칙 점검만으로 채점 |

## 7. 알려진 한계

- 이미지와 스캔 PDF는 AI OCR이 꺼져 있으면 채점할 수 없다.
- AI 심층 분석은 같은 서류라도 실행마다 결과가 조금 달라질 수 있다. 규칙 단계 점수는 항상 같다.
- 백그라운드 작업은 서버 프로세스 안에서 돌기 때문에 채점 중 서버를 재시작하면 그 심사는 실패로 처리된다.
- 점수는 운영용 점검 기준에 따른 값이며 IRB 승인 가능성을 보장하지 않는다.
