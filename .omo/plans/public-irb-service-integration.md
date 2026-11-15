# 공용 IRB 문서 양식과 평가기준 서비스 통합 계획

연구자는 과제의 위원회와 신청 경로를 정하면 필요한 파일·외부 입력 항목·후속 보고를 한 흐름에서 확인한다. 심의자는 같은 과제 버전의 제출 자료와 유형별 평가 항목을 보며 판단과 근거를 저장한다. 기존 자체 IRB의 서식과 기준은 과제 맥락에 따라 선택한다.

## 근거와 판정 원칙

- [Notion 조사 본문](https://app.notion.com/p/0637ecb51a4283899a4801c8123f35bf) · [연구계획서](https://app.notion.com/p/0c87ecb51a42834d80e281126c8b360c) · [설명문·동의서](https://app.notion.com/p/6017ecb51a4282d094c58139c1fc6c28) · [결과보고서](https://app.notion.com/p/2f07ecb51a4282c2ba07012c7f209664) · [추가 서류](https://app.notion.com/p/2d87ecb51a42831f8f8201fc3a43e2c0) · [평가기준표](https://app.notion.com/p/20a7ecb51a428274aedc81ca1fe8ef58)
- [국가생명윤리정책원 안내](https://nibp.kr/page/portal206)와 Notion 첨부 표준운영지침 ver.5.5를 근거로 삼는다. 공개 게시물과 첨부 파일의 최신성, 현재 e-IRB 화면의 요구사항은 구현 1단계에서 버전별 대조한다.
- 연구계획서 제38·39·40·41·43호, 설명문·동의서 제44·45호, 아동용 제46·47호, 결과보고서 제57호를 유형별로 연결한다. Notion의 변경대비표 제4-1호와 저장소 자료가 가리키는 제8호는 판본을 확인할 때까지 별개로 보존한다.
- Notion 첨부 제10-5호는 특정 신규 연구용, 제10-6·10-7·10-8호는 개인정보 제공·인체유래물 제공·폐기/이관용이다. 별지 제20호는 현장조사, 제13호는 면제확인서다. 일반 신규과제용 점검표는 이 목록의 첨부만으로 채우지 않는다.
- 상위 페이지의 심의 의견과 정규·신속심의 표는 예시 설명으로 읽는다. 심의 유형은 의견과 다른 데이터로 유지한다. 첨부 지침의 “수정 후 신속 심의”를 승인 완료로 표시하지 않는다.
- 보건·윤리 기준의 적용을 등록하는 범위이며 실험이나 검체 처리 절차를 제공하는 기능은 포함하지 않는다.

## 서비스 적용 범위

| 영역 | 현재 | 목표 |
|---|---|---|
| 과제·워크플로 | 과제 심의 유형과 상태, QC 프로필이 분리됨 | 과제 기준 선택 → 제출 시 고정 → 심의·보고로 연결 |
| 양식·자동 작성 | 기관 템플릿은 문서 종류별 활성 1개, DOCX/HWPX 편집 | 같은 문서 종류의 공용·자체 서식과 연구 유형별 변형 공존 |
| 제출·패키지·QC | 기관 체크리스트와 공용 QC 요구표가 달리 계산됨 | 동일한 버전의 제출 요구표를 화면·API·ZIP/PDF에서 공유 |
| 사전검토·심의 | 위험도·면제/동의 권고·QC 점수 중심 | 공식 점검표의 항목 상태와 증거, 심의자 의견 및 공식 통지 분리 |
| 운영·이력 | 기준 YAML과 저장 결과 일부 버전만 기록 | 원문·공용 팩의 버전, 출처, 시행 범위 및 제출 당시 판단 재현 |

## Todos

- [ ] 1. 공식 원문과 적용 범위 등록
  - Recommended task executor category: unspecified-low
  - 작업: 파일별 문서 번호·판본·발행 주체·원문 SHA-256·출처·작성자·적용 상황·현행 여부를 등록한다. Notion 첨부의 평가표 제10-5~10-8호, 별지 제20호·제13호를 다른 유형과 구분한다. 제10-1~10-4호 및 나머지 유형은 공식 원문이 확보·검수된 경우에만 활성화한다.
  - 의존: —
  - 참조: Notion의 HWP/PDF, NIBP 공식 자료실, docs/reference/공용IRB/98개_시나리오별_제출서류.md
  - 완료 및 QA: 현행 서식과 보관 서식의 동일 번호가 충돌하지 않고, 근거 없는 항목은 출처 확인 대기로 남는다. 저장소 루트에서 `.venv/Scripts/python.exe -m pytest -q` 실행. 해당 화면·API 또는 원문 대조 결과를 `.omo/evidence/public-irb/task-1.md`에 기록한다.
  - 커밋 단위: 공식 원문과 적용 범위 등록

- [ ] 2. 과제별 기준과 제출 경로 결정
  - Recommended task executor category: unspecified-high
  - 작업: 과제에 공용/자체 IRB 및 이용 경로를 명시한다. 연구 유형, 신규/변경/지속/종료·조기 종료/사건/제공/폐기·이관, 동의 분기, 다기관을 구조화한다. 기존 과제는 자체 IRB 기본을 유지하며 공용은 명시 선택한다. 제출 시 기준 버전과 선택 값을 스냅숏으로 고정한다.
  - 의존: 1
  - 참조: irb_automation/models.py, engine/facts.py, engine/ruleset.py, qc/profile.py, web/routes_protocols.py
  - 완료 및 QA: 자체 IRB 이전 과제가 종전 규칙을 사용하고, 공용 과제가 과제·API·QC에서 동일 컨텍스트를 사용한다. 저장소 루트에서 `.venv/Scripts/python.exe -m pytest -q` 실행. 해당 화면·API 또는 원문 대조 결과를 `.omo/evidence/public-irb/task-2.md`에 기록한다.
  - 커밋 단위: 과제별 기준과 제출 경로 결정

- [ ] 3. 제출 요구표를 하나의 계산기로 통합
  - Recommended task executor category: unspecified-high
  - 작업: 공용 카탈로그에서 경로별 항목을 계산하고 각 화면에 동일 결과를 넘긴다. 항목에는 필수/조건부/권고, 공식 파일/자유양식/외부 e-IRB 입력/위원회 작성, 작성 주체, 적용 조건, 근거, 상태를 둔다. QC의 선택 범위를 유지한다. 외부 e-IRB 입력은 내부 작성 지원과 실제 제출 확인을 구분한다.
  - 의존: 1, 2
  - 참조: engine/checklist.py, qc/bundle.py, qc/required_docs.yaml, package/service.py, pipeline/service.py, forms/online.py, submission/bundle.py
  - 완료 및 QA: 한 사례의 과제 상세·패키지·사전검토·QC·내보내기 요구표가 일치하고, 심의면제에 동의면제나 면제확인서가 자동 추가되지 않는다. 저장소 루트에서 `.venv/Scripts/python.exe -m pytest -q` 실행. 해당 화면·API 또는 원문 대조 결과를 `.omo/evidence/public-irb/task-3.md`에 기록한다.
  - 커밋 단위: 제출 요구표를 하나의 계산기로 통합

- [ ] 4. 서식 라이브러리와 자동 작성 연결
  - Recommended task executor category: unspecified-high
  - 작업: 활성 서식을 위원회·문서 유형·서식 변형 별로 선택한다. 원본 HWP는 버전이 붙은 참조 파일로 보존한다. 서식 수정을 지원하려면 검수한 HWPX/DOCX 파생본을 별도 등록하고 기존 필드 채우기 엔진을 사용한다. 서명·위원회 판단·확인 자료는 수동 입력으로 남긴다.
  - 의존: 1, 2, 3
  - 참조: doctemplates/models.py, doctemplates/service.py, doctemplates/fields.py, web/routes_doctemplates.py, forms/packs.py, docproc/text.py, package/service.py
  - 완료 및 QA: 조사형·개입형·개인정보 이용형이 각각 다른 계획서를 선택하고, 공용 템플릿 누락 시 자체 기관본이 섞이지 않는다. 출력의 표·체크박스·쪽 나눔을 원본과 대조한다. 저장소 루트에서 `.venv/Scripts/python.exe -m pytest -q` 실행. 해당 화면·API 또는 원문 대조 결과를 `.omo/evidence/public-irb/task-4.md`에 기록한다.
  - 커밋 단위: 서식 라이브러리와 자동 작성 연결

- [ ] 5. IRB 평가점검표와 근거 모델 구현
  - Recommended task executor category: unspecified-high
  - 작업: 공식 점검표를 섹션+번호 키로 저장한다. 폼별 질문 의미, 적용 조건과 해석, 근거 위치를 등록한다. 항목별 예/아니오/해당 없음과 내부 미확인·검토 대기를 구분한다. 존재 여부는 자동 후보로 올리고 타당성·적절성은 심의자 검토로 연결한다. 예/아니오의 방향을 항목별로 해석한다. QC 점수와 공식 평가표·공식 심의 결과를 각자 기록한다.
  - 의존: 1, 2, 3
  - 참조: engine/prereview.py, engine/findings.py, qc/checks.py, qc/units.py, qc/review.py, qc/scoring.py, api/routes.py
  - 완료 및 QA: 제10-6호 재제공 허용에 대한 예 응답이 자동 통과로 바뀌지 않고, 자료 누락은 미확인으로 남는다. QC PASS만으로 공식 결과가 생성되지 않는다. 저장소 루트에서 `.venv/Scripts/python.exe -m pytest -q` 실행. 해당 화면·API 또는 원문 대조 결과를 `.omo/evidence/public-irb/task-5.md`에 기록한다.
  - 커밋 단위: IRB 평가점검표와 근거 모델 구현

- [ ] 6. 신청 후 심의와 보고 업무 연결
  - Recommended task executor category: unspecified-high
  - 작업: 심의 유형 권고, 심의자 의견, 후속 재심의 경로, 최종 결과를 분리한다. 표준운영지침과 실제 공용 화면 확인 후 변경·지속·종료/조기 종료·사건·제공·폐기/이관 상태를 과제에 연결한다. 신청 서류와 위원회 통지문 권한을 분리하고 수신한 외부 결과는 출처·발급일을 기록한다.
  - 의존: 2, 3, 5
  - 참조: workflow/service.py, models.py, web/routes_protocols.py, web/routes_reports.py, package/revision.py, submission/bundle.py, notifications.py
  - 완료 및 QA: 승인 전 계획 변경과 이미 발생한 이탈 보고가 다른 업무로 생성된다. 연구자는 면제확인서를 발급할 수 없다. 제출 당시 기준의 결과를 다시 열 수 있다. 저장소 루트에서 `.venv/Scripts/python.exe -m pytest -q` 실행. 해당 화면·API 또는 원문 대조 결과를 `.omo/evidence/public-irb/task-6.md`에 기록한다.
  - 커밋 단위: 신청 후 심의와 보고 업무 연결

- [ ] 7. 전 경로 회귀 검증과 활성화 절차 마련
  - Recommended task executor category: unspecified-high
  - 작업: 98개 시나리오 자료를 요구사항 매핑에 사용한다. 핵심 예외의 자동 재현을 추가하고 전체 경로의 공식 근거 유무 및 상태 전이를 검증한다. SQLite 기존 자료와 Docker 영속 볼륨 복제본에서 마이그레이션·이전 버전 재현·활성 기준 복귀를 확인한다.
  - 의존: 3, 4, 5, 6
  - 참조: tests/test_engine.py, tests/test_pipeline_web.py, tests/test_qc_feedback.py, tests/test_doctemplates.py, tests/test_submission.py, tests/test_workflow.py, docker-compose.yml
  - 완료 및 QA: 전체 pytest·ruff·qc-eval 통과, 정상/미확인/잘못된 권한 예제 검증, 이력과 기준 스냅숏 보존. 저장소 루트에서 `.venv/Scripts/python.exe -m pytest -q` 실행. 해당 화면·API 또는 원문 대조 결과를 `.omo/evidence/public-irb/task-7.md`에 기록한다.
  - 커밋 단위: 전 경로 회귀 검증과 활성화 절차 마련

## Final verification wave

- [ ] F1. 98개 경로 매핑에서 근거 확인·미확인과 자료 역할을 대조한다.
- [ ] F2. 신규·면제·동의면제·변경·이탈·제공·폐기·종료와 자체 IRB의 데이터 및 접근 권한을 검증한다.
- [ ] F3. Docker에서 연구자 신청, 심의자 검토, 보고서 다운로드, 기준 변경 후 이전 결과 조회를 실제 화면에서 확인한다.
- [ ] F4. `pytest`, `ruff`, `qc-eval`과 복제한 SQLite 마이그레이션 및 롤백 검증 결과를 기록한다.

## 출시 조건

공용 팩은 원문 검수와 회귀 검증을 완료한 유형만 활성화한다. 근거 미확인 유형은 자료 상태를 화면에 보여 주고 담당자가 확인할 수 있게 한다. 서식의 현행성이나 포털 연계 권한이 확인되지 않은 경우 링크와 준비 자료로 안내한다. 법령·지침의 해석, 실제 접수 및 최종 승인 여부는 관할 위원회의 확인 기록으로 관리한다.
