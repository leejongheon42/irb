# AI 문서 자동화 파이프라인

병원마다 서식이 다른 연구계획서·제출 서류를 **인식 → 분류 → 개인정보 점검 → 정보 추출 → 변환 → 정합성 점검 → 사전검토 → 관련 정보 검색 → AI 검토 요약**까지 자동 처리한다.
모든 단계는 **규칙 기반으로 오프라인에서 끝까지 동작**하며, AI(Claude)를 켜면 OCR·추출·분류·요약 품질이 올라간다.

## 1. 화면

| 경로 | 대상 | 내용 |
|---|---|---|
| `/protocols/import` | 연구자·사무국 | 타 기관 서식 계획서 업로드 → 분석 → 항목별 체크(신뢰도·근거 문장) → 새 과제(작성중) 생성 + 원본을 '연구계획서'로 첨부 → 수정 화면 |
| `/protocols/{pid}/pipeline` | 과제 열람 권한자 | 분석 이력, 재분석, 추출 제안을 기존 과제에 선택 반영(빈 칸만 / 덮어쓰기, 연구진·편집 가능 상태만) |
| `/pipeline/{run_id}` | 과제 열람 권한자(가져오기는 실행자·사무국) | 단계별 진행·결과. 실행 중이면 `pipeline.js`가 `/api/pipeline/{run_id}`를 폴링, JS가 없으면 새로고침 안내 |
| `/pipeline/{run_id}/markdown`, `/pdf` | 〃 | 변환 결과 내려받기(PDF는 요청 시 생성) |
| `/admin/ai` | 간사·위원장·관리자 | AI 상태, 전송 정책, 최근 분석, AI 호출·전송 기록(해시·크기만) |

## 2. 단계

| # | 단계 | 규칙 기반 | AI 사용 시 |
|---|---|---|---|
| 1 | 문서 인식 | PDF 텍스트층(pypdf), DOCX·HWPX(ZIP XML, 문단·표를 "항목 \| 내용"), HWP5(OLE + BodyText 레코드 PARA_TEXT, zlib), XLSX, HTML, TXT(UTF-8/CP949), DOC·RTF·ODT(LibreOffice→PDF). 내용 SHA-256 기준 캐시 | 스캔 PDF·이미지 OCR(허용 서류 + 개인정보 미탐지 시에만 원본 전송) |
| 2 | 서류 분류 | 서식 키워드·제목 영역·파일명·계획서 구성 점수. 업로드 종류와 다르면 경고 | 신뢰도 0.6 미만이면 AI 보조 분류 |
| 3 | 개인정보 점검 | 주민번호·전화·이메일·주소·카드·여권·호칭형 이름. 심각하면 사무국 알림 | — |
| 4 | 정보 추출 | 제목 별칭 사전 + 번호 체계(1. / 1.2 / 가. / ① / Ⅰ. / 제n조) + 인라인·표 + 날짜·숫자 정규화 + 키워드 추정(부정 표현 제외) + 기관 고유 항목(`forms.schema.active_protocol_fields`) | 구조화 출력으로 추출, 인용문이 원문에 있으면 신뢰도 0.8 → 규칙 결과와 병합(값이 같으면 신뢰도↑, 다르면 대안 표시) |
| 5 | 형식 변환 | 마크다운 저장, PDF(원본 PDF 그대로 / LibreOffice / 추출 텍스트로 생성) | — |
| 6 | 정합성 점검 | 과제 입력 vs 문서: 대상자 수(10% 초과), 연구·자료 기간, 동의 방식, 식별자·출처·민감정보·취약군 누락, 반출·국외·결합·AI, 보관 기간, 제목 | — |
| 7 | 사전검토 | 규칙 엔진(`wf.preview`, 가져오기는 추출값으로 임시 계산) | — |
| 8 | 관련 정보 검색 | `knowledge.service.related_for_findings`, `similar_protocols`(모듈 없으면 건너뜀) | — |
| 9 | AI 검토 요약 | 건너뜀 | 요약·핵심·확인 필요·누락·질의(참고용) |
| 10 | 서식 작성 제안(가져오기) | 의뢰서·동의면제·가명처리·CRF·보안서약·참여기관 | — |

단계가 실패해도 다음 단계는 가능한 범위에서 계속하며 실행 상태는 `succeeded / partial / failed`.

## 3. 실행 방식

- `start_document_pipeline(db, user, document_id)` — 서류 저장 직후(`submission.hooks.after_document_saved`) 호출. `IRB_AI_PIPELINE_AUTO=false`면 아무것도 하지 않음
- `start_import_pipeline(db, user, filename, raw)`, `start_protocol_pipeline(db, user, protocol_id)`
- 실행은 **호출한 세션이 커밋된 뒤** 백그라운드 스레드(ThreadPoolExecutor 2개)에서 별도 DB 세션으로 수행. `IRB_PIPELINE_SYNC=true`면 즉시 커밋 후 동기 실행(테스트·디버그)
- 모델: `PipelineRun`, `PipelineStep`(단계별 상태·메시지·결과 JSON·소요시간), `ExtractedTextCache`

## 4. Claude 연동 (`irb_automation/ai/client.py`)

- 모든 AI 기능은 이 모듈만 거친다: `create_message`, `structured_call`, `text_call`, `pdf_block`, `image_block`, `text_document_block`
- 모델 `IRB_AI_MODEL`(기본 `claude-opus-5-5`), thinking 생략(적응형), 호출별 `effort` 명시
- 서버측 폴백 `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`
- 구조화 출력 `output_config.format = {"type": "json_schema", ...}`
- `stop_reason` refusal / max_tokens → `AIError`. SDK 예외는 구체 타입부터 `AIError`/`AIUnavailable`로 정규화
- `max_tokens` > 16000(OCR)은 스트리밍(`get_final_message`)
- 비활성·SDK 없음·자격증명 없음 → `AIUnavailable` → 규칙 경로로 계속
- 기존 `engine.ai_review.run_ai_review`도 이 클라이언트와 개인정보 가드를 사용

## 5. 개인정보 보호 (`irb_automation/ai/privacy.py`)

- 텍스트는 `deid.patterns` 기반 탐지 후 **마스킹하여 전송**. 연구 기간 등 날짜는 보존
- 연구 문서 오탐 보정: '신장병 환자'처럼 질병·집단명 + 호칭은 이름으로 보지 않고, '환자로 1년'처럼 조사 '로' + 숫자·단위는 도로명 주소로 보지 않음
- 원본 PDF·이미지는 `IRB_AI_DOC_TYPES_ALLOWED`(기본 protocol, application, crf, deid_plan)이고 개인정보가 탐지되지 않을 때만 전송
- 전송마다 감사 로그 `ai_transmit`: 목적, 대상, 형식, SHA-256, 바이트, 마스킹 건수, 모델. **내용은 기록하지 않음**

## 6. 설정

| 변수 | 기본 | 설명 |
|---|---|---|
| `IRB_AI_ENABLED` / `IRB_AI_REVIEW_ENABLED` | false | AI 기능 전체(기관 승인 후) |
| `IRB_AI_MODEL` | claude-opus-5-5 | 모델 |
| `IRB_AI_PIPELINE_AUTO` | true | 업로드 시 자동 분석 |
| `IRB_AI_DOC_TYPES_ALLOWED` | protocol,application,crf,deid_plan | 원본 전송 허용 서류 |
| `IRB_SOFFICE_PATH` | PATH 자동 탐지 | LibreOffice |
| `IRB_PIPELINE_SYNC` | false | 동기 실행 |

기관 고유 서식 명칭은 규칙 세트 `document_pipeline.field_aliases`(섹션 제목 별칭), `document_pipeline.classify_keywords`(분류 키워드)에 추가한다.

## 7. 샘플·테스트

- `python samples/generate_documents.py [출력 디렉터리]` — 가상 병원 3곳의 서로 다른 서식(DOCX 표+번호형, HWPX 로마숫자·원문자형, HWP5 바이너리, 텍스트층 PDF). 실제 환자 정보 없음
- `tests/test_pipeline.py`, `tests/test_pipeline_web.py` — 형식별 인식, HWP 레코드 파서, 분류, 제목 변형 추출, 정합성, PII 가드, 가짜 클라이언트로 AI 파라미터·거절·예외, 동기·백그라운드 파이프라인, 가져오기→과제 생성, 권한

## 8. 한계

- 스캔본·이미지는 AI 없이는 글자를 읽지 못한다(로컬 OCR 엔진 미포함)
- 배포용·암호 HWP는 미리보기 텍스트만 읽는다
- 키워드 추정 항목(연구 유형, 데이터 출처, 식별자 등)은 신뢰도 50~60%로 표시되며 연구자 확인이 필요하다
- 백그라운드 실행은 프로세스 내 스레드라 서버 재시작 시 진행 중인 실행은 '진행 중'으로 남는다(재분석으로 다시 실행)
