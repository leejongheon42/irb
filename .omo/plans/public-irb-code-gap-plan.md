# 공용 IRB 통합 계획 보강안 (현재 코드 대조)

대상: `.omo/plans/public-irb-service-integration.md`의 Todo 1~7.
기준: 2026-11-14 작업 트리(미커밋 QC 변경 포함)와 Notion "IRB 양식 조사·정리" 및 하위 5개 페이지.

기존 계획의 방향은 유지한다. 이 문서는 코드를 읽어 확인한 전제 불일치(A), 기존 계획에 없는 추가 작업(B), 기존 Todo별 보강 내용(C), 확정한 결정(D)을 적는다.

## A. 기존 계획의 전제와 코드가 다른 곳

| # | 계획의 전제 | 코드의 실제 | 영향 |
|---|---|---|---|
| A1 | 심의자가 판단과 근거를 저장한다 | 역할은 `RESEARCHER`·`ADMIN` 둘뿐이다(`models.py:28`). 심의는 외부 e-IRB에서 하고 연구자가 결과를 옮겨 적는다(`workflow/service.py:1-9`, `record_result`는 `require_member`) | Todo 5·6의 심의자 화면·권한을 얹을 주체가 없다 → D1 |
| A2 | 과제에서 공용/자체를 선택한다 | `Protocol`에 위원회 필드가 없다. QC는 과제가 연결되면 무조건 `institution`, 아니면 `public_irb`로 추정한다(`qc/profile.py:40`). 공용 선택은 QC 폼 답변으로만 가능하다(`web/routes_package.py:37`) | 과제에 묶인 공용 사례는 패키지·사전검토·ZIP에서 항상 기관 규칙을 탄다 |
| A3 | 연구 유형으로 계획서 38·39·40·41·43호를 고른다 | `study_type` 카탈로그는 후향적 차트·CDW·레지스트리 등 데이터 연구 8종이다(`default_ruleset.yaml:57`). 개입·인체유래물·배아줄기세포주가 없다. QC 프로필은 별도 어휘(`human/specimen/both` + `survey/intervention/records/stem_cell`)를 쓴다 | 두 어휘의 대응표 없이는 서식 변형을 고를 수 없다 |
| A4 | 변경·이탈·사건·제공·폐기를 과제에 연결한다 | `SubmissionKind`는 5종이고 `next_submission_kind`가 과제 상태에서 단일 경로를 유도한다(`workflow/service.py:144`). 제출하면 과제 상태가 `SUBMITTED`로 바뀐다 | 승인 상태를 유지한 채 병행되는 보고(이탈·이상반응·이해상충 변경·제공·폐기)를 담을 수 없다 |
| A5 | 제출 당시 기준으로 재현한다 | `Submission.snapshot`은 과제 필드만, `PreReview.ruleset_version`은 `버전#해시8자리`만 남긴다. YAML 본문은 보관하지 않고 `required_docs.yaml`·`criteria.yaml`·서식·양식 정의 버전은 제출에 묶이지 않는다 | 기준 파일이 바뀌면 이전 판단을 다시 계산할 수 없다 |
| A6 | 요구표 계산기를 하나로 합친다 | 계산기가 둘이고 조건 어휘도 다르다. `engine/checklist.py`는 `ProtocolFacts` 람다 16개, `qc/bundle.py`는 프로필 조건 집합. 체크리스트 항목은 `doc/label/provided/condition`뿐이다 | 통합 전에 공통 조건 어휘와 항목 스키마를 먼저 정해야 한다 |
| A7 | 같은 문서 종류의 공용·자체 서식이 공존한다 | `activate`가 같은 `doc_type`의 다른 활성본을 끈다(`doctemplates/service.py:196`). 채우기 엔진은 DOCX·HWPX만 있다. Notion 원본은 전부 HWP 5.x(1건은 PDF) | 모델 변경과 HWP→HWPX 파생본 제작·검증 절차가 필요하다 |
| A8 | 공용 서식 누락 시 자체본이 섞이지 않는다 | `documents.build_document`는 활성 서식이 없으면 내장 HTML 서식으로 내려간다 | 공용 과제에서는 이 폴백을 막아야 한다 |
| A9 | 마이그레이션과 롤백을 검증한다 | `migrate_columns`는 컬럼 추가만 한다(`database.py:80`). 마이그레이션 도구와 스키마 버전 기록이 없다 | 롤백은 DB 파일 백업·복원 절차로 정의해야 한다 |

## B. 기존 계획에 없는 추가 작업

- [ ] B0. 작업 트리 기준선 커밋
  - QC 관련 미커밋 변경이 20개 이상이고 `qc/bundle.py`, `qc/required_docs.yaml`, `qc/profile.py` 등 통합 대상이 미추적 상태다. Todo별 커밋 단위를 지키려면 현재 상태를 먼저 커밋한다(`pytest`·`ruff` 통과 확인 후).
  - 의존: 없음. 모든 Todo보다 먼저.

- [ ] B1. 서식 번호 체계를 (계열, 번호, 판본)으로 저장
  - "제8호"가 두 문서를 가리킨다. 권고서식 제8호는 변경대비표(`98개_시나리오별_제출서류.md:43`), 별지 제8호는 연구계획위반이탈사례 보고서(Notion 추가 서류). 기존 계획 9행은 제4-1호와 제8호를 같은 문서의 판본 후보로 다루는데, 계열을 구분하면 권고 제4-1호(e-IRB용 ver 4.0)·권고 제8호는 변경대비표, 별지 제8호는 별개 문서다.
  - 같은 충돌: 별지 제10-1호(이해상충공개서 연구자용)와 권고 제10-5~10-8호(평가점검표). 기존 계획이 "제10-1~10-4호"를 점검표로 가정한 부분은 계열 확인 전까지 보류한다.
  - 키: `series`(권고서식/별지/공용위원회 별지/시행규칙 별지) + `number` + `version`. 번호 문자열 단독 조회를 금지한다.
  - 의존: Todo 1에 포함.

- [ ] B2. Notion에 있으나 기존 계획에 빠진 서식 등록
  - 연구자 자가점검표 2종: 권고 제11호 서면동의면제 자가점검표, 권고 제3호 심의면제 자가점검표(PDF). 위원회용 평가점검표와 작성 주체가 다르므로 Todo 5 모델에 `owner=researcher` 유형으로 넣는다.
  - 신청·사유 서식: 별지 제22호 서면동의면제사유서, 별지 제12호 심의면제신청서, 별지 제24호 협약서(기관 비소속 연구자), 공용위원회 별지 제28-3호 폐기·이관 심의 신청서 ver 4.3.
  - 폐기·이관의 자유양식 부속자료 4종(계획서, 설명문·동의서 사본, 관리대장, 위원회 요청 문서)은 "위원회 요청 시" 조건으로 둔다.
  - 저장소 참조표는 22·12·24·28-3호를 "구 별지"로 본다. Notion 첨부와 현행성이 다르므로 `현행 여부=확인 대기`로 등록한다.
  - 판본은 파일명에서 얻는다: 38·39·40호 ver 4.0, 41호 ver 3.0, 43호 ver 2.0, 44·45호 ver 5.0, 46·47호 ver 2.0, 57호 ver 1.0, SOP ver 5.5.

- [ ] B3. 원본 파일 보관 방식
  - Notion 파일 URL은 만료되는 서명 주소다. 원본을 데이터 디렉터리에 SHA-256 이름으로 저장하고 레지스트리에는 Notion 블록 ID와 취득일을 남긴다.
  - Notion 페이지는 미검증 상태이고 심의 종류 표는 의견과 종류를 뒤섞어 적었다. 레지스트리의 `검수자·검수일`이 비어 있으면 활성화하지 못하게 한다.

- [ ] B4. 이용 경로와 협약 자료
  - 경로 3종(기관협약, 개인연구자, 기관장 합의)에 따라 선행 확인과 증빙이 다르다(참조표 공통 A).
  - Notion의 협약 기관 리스트(xlsx, 26.09.30)를 조회 자료로 넣고 기준일을 표시한다. 과제 소속 기관이 목록에 있으면 기관협약 경로를 제안만 하고 확정은 사용자가 한다.
  - 심의비는 결제 절차이므로 요구표 항목이 아닌 안내로 표시한다.

- [ ] B5. 대상자 연령·동의 구조화
  - 아동용 승낙서(46·47호)는 만 7~12세 조건이다. 현재는 `vulnerable_populations` 목록과 QC 본문 정규식(`qc/profile.py:54`)뿐이라 연령대를 알 수 없다.
  - 과제에 대상자 연령 범위와 대리인 동의 여부를 추가한다. 대상군별로 동의 방식이 다른 사례(시나리오 7·23)를 위해 동의 경로를 복수 선택으로 둔다(현재 `consent_plan`은 `obtain|waiver` 단일값).

- [ ] B6. 기한 알림
  - 이해상충 변경은 인지 후 30일 내 보고(Notion 추가 서류). `run_scheduler`(`workflow/service.py:476`)에 이해상충 변경 기록 후 30일, 이탈·이상반응 발생일 기준 알림을 추가한다. 이탈·이상반응 기한 수치는 SOP ver 5.5 조문 확인 후 넣는다.

- [ ] B7. 요구표 API
  - `api/routes.py`에는 평가·과제 목록·사전검토만 있다. `GET /api/protocols/{id}/requirements`를 추가해 화면과 같은 계산 결과를 낸다. Todo 3 QA의 "화면·API 일치"를 이 엔드포인트로 검증한다.

## C. 기존 Todo별 보강

### Todo 1 — 원문 등록
- 저장 위치: 새 테이블 `source_documents`(series, number, version, title, issuer, sha256, stored_path, origin, acquired_on, applies_to, status, verified_by, verified_at). `status`는 `current|archived|unverified`.
- `qc/required_docs.yaml` 각 항목의 `form`·`basis`를 레지스트리 키로 바꾸고 `verified: false`를 기본값으로 둔다. 파일 머리말이 이미 "원문 미검수"라고 밝히고 있으므로 항목 단위로 옮긴다.
- 원문 대조 자동화: `docproc/text.py`가 HWP 5.x 본문을 읽으므로 등록 시 표지 문구에서 서식 번호·판본을 추출해 입력값과 비교한다. 배포용·암호 문서는 미리보기만 읽히므로 그 경우 수동 확인으로 표시한다.

### Todo 2 — 과제별 기준
- `Protocol` 추가 컬럼(문자열, 기본값은 기존 동작): `committee`=`institution`, `access_route`=``, `research_category`=``(human|specimen|both|stem_cell), `research_method`=[] , `subject_age_min/max`, `consent_routes`=[]. `migrate_columns`가 기본값을 채우므로 기존 과제는 자체 IRB로 남는다.
- `PROTOCOL_FIELDS`(`engine/facts.py`)와 `ProtocolFacts`에 같은 필드를 넣어 스냅숏에 포함시킨다.
- `qc/profile.py:40`의 추정을 `protocol.committee` 읽기로 바꾸고 `source`를 `record`로 표시한다. `study_type → (category, method)` 대응표를 한 곳에 두고 양쪽이 쓴다.
- 기준 고정: 새 테이블 `rule_packs`(kind, sha256, version, content)에 YAML 원문을 내용 주소로 저장하고 `Submission`에 `basis`(JSON: ruleset·required_docs·criteria·checklist 팩의 sha, 서식 id·sha, 양식 정의 버전)를 추가한다. 재현 시 `get_ruleset()` 대신 저장된 본문으로 계산한다.

### Todo 3 — 요구표 통합
- 순서: ① 공통 항목 스키마(`doc, label, level, channel, owner, when, basis, source_ref, status, verified`) → ② 조건 어휘 통합(`CONDITIONS` 16개와 프로필 조건을 하나의 평가 함수로) → ③ `requirements.compute(context)` 신설 → ④ `build_checklist`와 `inspect_bundle`을 이 함수의 어댑터로 전환 → ⑤ 소비처 이전(`package/service.plan`, `workflow.preview`, `submission/service.build_board`, `pipeline.step_prereview`, `package.build_zip`, `submission/bundle`).
- 기관 규칙의 기존 결과가 바뀌지 않는지 `tests/test_engine.py` 체크리스트 테스트를 그대로 통과시켜 확인한다.
- 함께 고칠 것:
  - `provided`가 `doc_type in uploaded_doc_types`라 이전 버전·보완요청 문서도 충족으로 센다. `is_current`와 문서 상태를 반영한다.
  - `kind_initial`이 변경심의를 포함해 변경 때 신규 서류 전체를 다시 요구한다(`checklist.py:26`). 공용 경로에서는 변경대비표와 변경된 문서만 요구한다.
  - `_REPORT_KIND_DOCS` 하드코딩을 카탈로그 조건으로 옮긴다.
  - 외부 e-IRB 입력 항목은 상태 3단계(내부 준비됨 / 제출했다고 표시 / 해당 없음)를 저장할 곳이 없다. 현재 `bundle_answers`는 QC 실행에만 남으므로 과제 단위 `requirement_states` 테이블로 옮긴다.

### Todo 4 — 서식 라이브러리
- `DocumentTemplate`에 `committee`, `variant`, `source_document_id`, `derived_from_sha` 추가. 활성 유일성 범위를 `(committee, doc_type, variant)`로 바꾼다(`activate`, `active_template`, `versions`, `documents.generatable_doc_types`).
- 공용 과제에서 활성 서식이 없으면 내장 HTML로 내려가지 않고 "원본 서식 내려받기 + 직접 작성"으로 안내한다(`documents.build_document`).
- HWP→HWPX 파생본 검증: 원본 HWP와 파생 HWPX를 `docproc.text`로 추출해 정규화한 문단 차이가 자리표시자 외에 없는지 자동 비교하고 결과를 서식 등록 화면에 보여 준다. 표·체크박스·쪽 나눔은 PDF 변환본 육안 대조로 남긴다(`.omo/evidence/public-irb/task-4.md`).
- HWPX 변환은 한글 프로그램에서 수동으로 한다. 코드로 자동 변환하지 않는다.
- 자리표시자 매핑(`doctemplates/fields.py`)에 Todo 2의 신규 필드를 추가한다.

### Todo 5 — 평가점검표
- 새 테이블: `checklist_forms`(source_document_id, owner, items JSON), `checklist_responses`(protocol_id, submission_id, form_id, item_key, answer, direction 해석 결과, evidence_ref, auto_candidate, decided_by, decided_at).
- `answer`는 `yes|no|na|unknown|pending`. 항목 정의에 `favorable: yes|no|none`을 두어 "예"가 통과가 아닌 항목(제10-6호 재제공 허용)을 표현한다.
- 판정 전이는 `qc/review.py`의 `transition` 구조를 재사용한다. 자동 후보는 `auto_candidate`에만 쓰고 `answer`는 사람이 확정한다.
- 확정 권한(D1): `owner=committee` 양식은 배정된 사전검토자만, `owner=researcher` 양식은 과제 구성원만 `answer`를 저장한다.
- `QCRun.verdict`와 분리: 점검표 화면에 QC 점수를 참고로만 표시하고 `SUBMISSION_RESULTS` 입력 경로와 연결하지 않는다.
- 10-5~10-8호 원본이 HWP이므로 항목 추출은 `docproc.text`의 표 행 출력("항목 | 내용")을 초안으로 쓰고 사람이 검수한다.

### Todo 6 — 신청 후 업무
- `SubmissionKind` 확장: `deviation`, `adverse_event`, `early_termination`, `coi_update`, `info_provision`, `specimen_provision`, `disposal_transfer`. 컬럼 길이 20자 안에 들어간다.
- 병행 보고는 과제 상태를 바꾸지 않는다. `_create_submission`의 `protocol.status = S.SUBMITTED`를 종류별로 분기하고 `current_submission`(마지막 제출 1건) 가정을 "열린 제출 목록"으로 바꾼다. `record_result`·`record_revision_request`가 대상 제출을 인자로 받게 한다.
- 변경대비표 대 이탈 보고 구분: `start_amendment` 진입 시 "이미 시행했는가"를 묻고 예이면 `deviation` 보고로 보낸다.
- 결과 어휘: `SUBMISSION_RESULTS`에 SOP의 의견 5종(승인, 수정 후 승인, 수정 후 신속심의, 보완, 반려)을 공용 전용 집합으로 추가하고 `revision`·`approved_with_conditions`와의 대응을 명시한다. "수정 후 신속심의"는 승인 상태로 전이하지 않는다.
- 통지 기록: `Submission`에 `result_source`(통지서 문서 id), `result_issued_on`, `result_entered_by`. 면제확인서·결과통지서 `doc_type`은 업로드만 허용하고 생성 대상(`GENERATABLE`)에서 제외한다.

### Todo 7 — 회귀·활성화
- 98개 시나리오를 테스트 픽스처로 변환: `완결_경로_시나리오.txt`의 각 줄이 `연구 유형 / 심의기관 / 참여기관 / 방식 / 조건 / 신청 경로 / 동의` 고정 형식이라 파싱할 수 있다. 시나리오별 기대 요구표는 `98개_시나리오별_제출서류.md`에서 수작업으로 10~15개 핵심 사례만 먼저 고정한다(1, 5, 8, 17, 21, 24, 25, 26, 29, 30, 33 + 변경·이탈·종료).
- 한계가 표시된 40~48번은 "근거 미확인" 상태가 화면에 나오는지만 검증한다.
- 롤백 절차: 활성화 전 SQLite 파일과 업로드 디렉터리를 복사해 두고 복원한다. `schema_meta` 테이블에 적용 버전을 기록한다. Docker 볼륨 `irb-data` 복제본에서 리허설한다.
- 활성화 스위치: 설정에 `public_irb_enabled`와 유형별 활성 목록을 두고 꺼져 있으면 과제 폼에 공용 선택지가 나오지 않게 한다.

## D. 확정한 결정 (2026-11-14)

### D1. 내부 사전검토자 역할을 추가한다

사전검토자는 연구자가 외부 IRB에 제출하기 전에, 기관 안에서 제출 자료를 평가점검표 항목과 대조해 항목별 판정을 확정하는 사람이다. 연구지원 부서 담당자나 기관 IRB 간사가 맡는다. 공용위원회의 심의위원을 대신하지 않으며, 이 사람의 판정은 "내부 사전검토 결과"로만 기록된다.

- 역할: `Role.REVIEWER`, 표시 이름 "사전검토자".
- 배정: 관리자가 과제별로 지정한다. `Protocol.reviewer_ids`(JSON 목록, 기본 `[]`)에 저장한다. `co_investigators`와 같은 방식이라 `migrate_columns`로 추가된다.
- 할 수 있는 일: 배정된 과제와 문서 열람, 점검표 항목의 `answer` 확정과 근거 위치 기록, 연구자에게 내부 보완 의견 남기기.
- 할 수 없는 일: 과제 내용·문서 수정, 외부 IRB 결과 입력(`record_result`), 면제확인서·결과통지서 등록, 서식·기준 관리.
- 이해상충 차단: 해당 과제의 연구책임자·공동연구자는 그 과제의 사전검토자가 될 수 없다. 배정 시점과 판정 저장 시점에 모두 검사한다.
- 소규모 기관: 관리자가 사전검토자를 겸할 수 있다. 같은 차단 규칙을 적용한다.
- 연구자: 자기 과제에서는 자가점검표(권고 제3호·제11호)만 작성한다. 위원회용 평가점검표(10-5~10-8호)는 자동 후보와 사전검토자 판정을 읽기만 한다.
- 고칠 곳: `models.py`(Role, ROLE_LABELS, Protocol.reviewer_ids), `workflow/service.py`(`can_view`에 배정 검토자 추가, `require_reviewer` 신설), `web/deps.py`의 `require_roles` 사용처, `web/routes_admin.py`(배정 화면), `seed.py`(데모 계정), `notifications.py`(배정·판정 완료 알림).
- 테스트: 배정되지 않은 검토자의 열람 거부, 과제 구성원의 검토자 배정 거부, 검토자의 결과 입력 거부, 연구자의 평가점검표 확정 거부.

### D2. `study_type`은 유지하고 공용 과제에서만 분류·방법을 필수로 받는다

- 자체 IRB 과제는 지금 폼 그대로 둔다.
- `committee=public_irb`를 고르면 `research_category`(human|specimen|both|stem_cell)와 `research_method`(survey|intervention|records|collect 등, 복수)를 필수로 받는다. 기존 `study_type` 값에서 초깃값을 제안한다(예: `survey` → human + survey, `retrospective_chart_review` → human + records). 대응이 없는 값은 비워 두고 사용자가 고른다.
- 서식 선택 규칙: intervention → 38호, survey → 39호, specimen → 40호, stem_cell → 41호, records → 43호. 방법이 둘 이상이면 자동으로 하나를 고르지 않고 후보를 모두 보여 준다.

### D3. 자가점검표는 조건부 권고 항목으로 등록한다

- 권고 제3호(심의면제)와 제11호(서면동의면제)를 `level: conditional`, `verified: false`, `현행 여부: 확인 대기`로 등록한다.
- 요구표에서는 "권고 · 현행 확인 필요"로 표시하고, 없더라도 제출 차단이나 QC 감점에 넣지 않는다.
- e-IRB 로그인 화면에서 첨부 요구를 확인한 뒤 관리자가 `required`로 올린다. 올린 사실은 기준 팩 버전에 남는다.

## 실행 순서

B0 → Todo 1(+B1·B2·B3) → Todo 2(+D1 역할·D2 필드·B4·B5) → Todo 3(+D3 등록·B7) → Todo 4, Todo 5 병행 → Todo 6(+B6) → Todo 7.

Todo 4와 5는 서로 다른 파일(`doctemplates/` 대 신규 점검표 모듈)을 건드려 병행할 수 있다. Todo 3은 `package/service.py`·`qc/bundle.py`를 건드려 현재 미커밋 변경과 겹치므로 B0 이후에 시작한다.
