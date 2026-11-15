import fs from "node:fs/promises";
import { constants } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const runtime = "C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1112-737-0_26.1112.737.0_x64__3k8sg7r9htsxt/dependencies";
const skill = "C:/Users/tlqkf/.codex/plugins/cache/openai-primary-runtime/presentations/26.1112.11217/skills/presentations";
const root = process.cwd();
const build = path.join(root, ".omo", "presentation-build");
const { Presentation, PresentationFile } = await import(pathToFileURL(
  path.join(runtime, "node/node_modules/@oai/artifact-tool/dist/artifact_tool.mjs")).href);
const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });
const navy = "#13283C", muted = "#42576A", teal = "#007569", white = "#FFFFFF";
const night = "#102637", lightTeal = "#7EE0C5", lightText = "#D3E4E9";
const font = "Malgun Gothic";

function put(slide, value, x, y, w, h, options = {}) {
  const shape = slide.shapes.add({
    name: value.split("\n")[0], geometry: "textbox",
    position: { left: x, top: y, width: w, height: h },
    fill: "none", line: { fill: "none", width: 0 },
  });
  shape.text = value;
  shape.text.style = {
    typeface: font, fontSize: options.size ?? 25, color: options.color ?? navy,
    bold: options.bold ?? false, alignment: options.align ?? "left",
    verticalAlignment: "middle", insets: { top: 0, left: 0, right: 0, bottom: 0 },
  };
}

function head(title, subtitle, dark = false) {
  const slide = presentation.slides.add();
  slide.background.fill = dark ? night : "#F5F8FA";
  put(slide, title, 76, 56, 1130, 72, { size: 51, bold: true, color: dark ? white : navy });
  put(slide, subtitle, 78, 141, 1110, 50, { size: 25, color: dark ? lightText : muted });
  return slide;
}

function notes(slide, sections, detail, screenshot) {
  slide.speakerNotes.text = detail + "\n\n출처: docs/서비스_전체_정리.md " + sections
    + (screenshot ? "\n화면 예시: docs/images/" + screenshot : "");
}

function rows(slide, items, { x = 78, y = 226, width = 1120, split = 246, step = 91, dark = false } = {}) {
  items.forEach(([label, copy], i) => {
    const top = y + i * step;
    put(slide, label, x, top, split - 22, 41, { size: 26, bold: true, color: dark ? lightTeal : teal });
    put(slide, copy, x + split, top - 4, width - split, 70, { size: 26, color: dark ? white : navy });
  });
}

function stacked(slide, items, { x = 78, y = 225, width = 465, step = 123 } = {}) {
  items.forEach(([label, copy], i) => {
    const top = y + i * step;
    put(slide, label, x, top, width, 40, { size: 28, bold: true, color: teal });
    put(slide, copy, x, top + 45, width, 57, { size: 24, color: navy });
  });
}

async function sample(slide, name, alt) {
  const blob = await fs.readFile(path.join(root, "docs", "images", name));
  slide.images.add({ blob, alt, contentType: "image/png", fit: "contain",
    position: { left: 584, top: 211, width: 620, height: 392 } });
  put(slide, "서비스 화면 예시", 584, 621, 620, 29, { size: 16, color: muted, align: "right" });
}

{
  const slide = presentation.slides.add();
  slide.background.fill = night;
  put(slide, "서비스 소개", 80, 102, 1040, 48, { size: 27, color: lightTeal, bold: true });
  put(slide, "IRB 제출서류\n자동완성 서비스", 76, 184, 1120, 216, { size: 88, bold: true, color: white });
  put(slide, "의료데이터 연구를 위한 서류 작성, 제출 전 점검, 심의 이력 관리", 81, 449, 1090, 70,
    { size: 29, color: lightText });
  put(slide, "2026년 11월 15일 기준", 81, 605, 1000, 34, { size: 20, color: lightTeal });
  notes(slide, "§1", "병원 의료데이터(EMR, CDW, PACS 등)를 사용하는 연구자의 제출 준비를 지원하는 웹 서비스다. 공식 심의는 병원 또는 공용 e-IRB에서 진행한다.");
}
{
  const slide = head("IRB 신청 업무 흐름", "공식 심의는 외부 e-IRB에서 진행합니다. 서비스는 제출 준비와 이력을 관리합니다.");
  rows(slide, [
    ["01  과제 등록", "직접 입력하거나 기존 연구계획서에서 항목을 가져옵니다."],
    ["02  사전점검", "예상 심의유형, 위험도, 동의면제 요건, 필요 서류를 확인합니다."],
    ["03  서류 준비", "과제 정보를 채우고 서류끼리 비교해 교정합니다."],
    ["04  외부 제출", "ZIP을 내려받아 e-IRB에 제출하고 접수 이력을 남깁니다."],
    ["05  후속 업무", "공식 결과를 기록하고 보완, 변경, 지속심의와 종료에 대응합니다."],
  ], { split: 257, step: 78 });
  notes(slide, "§1, §3.1", "공식 결과는 외부 IRB가 발행한다. 서류 준비는 규칙 기반 경로로 처리할 수 있으며, AI는 선택 기능이다.");
}
{
  const slide = head("사용자 역할과 권한", "과제 열람 범위와 점검표 판정 권한은 역할 및 과제 배정에 따라 달라집니다.");
  rows(slide, [
    ["연구자", "참여 과제의 서류를 작성·점검하고 자가점검표,\n제출 기록, 외부 심의 결과를 관리합니다."],
    ["사전검토자", "배정된 과제의 위원회용 평가점검표에 판정과 근거를 남깁니다.\n자신이 연구진인 과제의 판정은 제한됩니다."],
    ["관리자", "전체 과제를 열람하고 사용자, 기관 양식, 점검 기준,\n공용 IRB 원문 및 감사 이력을 관리합니다."],
  ], { step: 125 });
  notes(slide, "§2, §3.13", "과제 구성원은 연구책임자와 공동연구자다. 사전검토자의 권한은 열람과 내부 평가로 제한된다.");
}
{
  const slide = head("기존 연구계획서 가져오기", "추출 근거를 확인한 뒤 필요한 항목만 과제에 반영합니다.");
  stacked(slide, [
    ["문서 인식", "PDF, DOCX, HWP·HWPX, XLSX, TXT의\n텍스트를 읽고 서류 종류를 분류합니다."],
    ["항목 추출", "제목, 기간, 대상자 수 등에 신뢰도와\n근거 문장을 붙입니다."],
    ["과제 반영", "연구자가 반영할 항목을 선택하고\n정합성 점검과 사전검토 결과를 봅니다."],
  ]);
  await sample(slide, "pipeline.png", "문서 분석 파이프라인의 단계별 결과 화면");
  notes(slide, "§3.7, §8", "개인정보 탐지, 마크다운·PDF 변환, 규정 검색을 포함한다. 스캔본 OCR과 AI 요약은 AI 사용 설정이 필요하다. 처리 중 서버가 재시작되면 실행을 다시 시작한다.", "pipeline.png");
}
{
  const slide = head("규칙 기반 사전점검", "과제 입력값을 바탕으로 예상 심의유형과 보완 항목을 제시합니다.");
  stacked(slide, [
    ["위험도 산정", "식별정보, 민감정보, 취약 대상, 반출 등\n위험 요인별 점수와 근거를 표시합니다."],
    ["심의유형·동의면제", "예상 유형과 서면동의 면제 요건을\n각각 확인할 수 있습니다."],
    ["필요 서류·검토 의견", "필요 서류와 중요도를 표시하고\n평가에 사용한 규칙 버전을 남깁니다."],
  ]);
  await sample(slide, "prereview.png", "예상 심의유형과 위험도를 표시하는 사전점검 화면");
  notes(slide, "§3.2", "규칙은 YAML로 관리한다. 결과는 내부 참고용이며 최종 심의유형과 승인 여부는 외부 IRB가 결정한다.", "prereview.png");
}
{
  const slide = head("제출 서류 패키지와 서류함", "이번 제출에 필요한 현행본을 확인하고 e-IRB용 파일을 내려받습니다.");
  rows(slide, [
    ["초안과 자동 작성", "빈 서술 항목에 초안을 제시하고 계획서, 동의서 등 지정 서식을 채웁니다."],
    ["직접 첨부", "교육 이수증, 이력서, 타기관 IRB 승인서 등 발급받은 자료를 올립니다."],
    ["문서 버전 관리", "현행본을 표시하고 이전 버전과 비교하거나 미리 볼 수 있습니다."],
    ["내보내기", "파일명에 번호와 버전을 붙인 ZIP, 목록, 점검 요약을 제공합니다.\n서류함에서는 표지와 목차를 포함한 PDF 합본도 만들 수 있습니다."],
  ], { split: 263, step: 98 });
  notes(slide, "§3.3, §3.6", "이미 작성된 서술 항목은 보존한다. 기관용 내장 서식은 LibreOffice가 있으면 DOCX, 없으면 HTML로 만든다. 파일 업로드 시 형식과 위험 요소를 검사한다.");
}
{
  const slide = head("서류 품질 점검과 자동 교정", "불일치를 확인하고 연구자가 기준값을 고르면 교정안을 만듭니다.");
  rows(slide, [
    ["규칙 점검", "필수 기재사항, 개인정보, 과제 정보와 문서 간 불일치를 찾습니다."],
    ["AI 심층 점검", "선택 기능으로 문맥 모순 등을 찾고, 인용문을 실제 원문과 대조합니다."],
    ["오탈자·숫자 검증", "잘못된 날짜, 기간 역전, 반복 단어와 표기 차이를 확인합니다."],
    ["교정 파일", "DOCX는 변경 추적본과 수정 완료본을 제공합니다.\nHWPX는 문단 내 텍스트를 교체합니다. PDF와 구형 HWP는 점검용입니다."],
  ], { split: 247, step: 94 });
  notes(slide, "§3.4", "100점에서 확정 Critical 15점, Major 7점, Minor 2점을 차감한다. AI 후보와 미확정 항목은 감점 대상에서 제외한다. DOCX 교정 시 표, 머리글, 바닥글을 유지한다. 과제에 적용한 수정본은 새 버전으로 기록한다.");
}
{
  const slide = head("제출 기록과 보완 대응", "외부 e-IRB에서 받은 접수와 통지 내용을 과제 이력에 연결합니다.");
  stacked(slide, [
    ["제출 당시 기록", "과제 스냅숏, 접수번호와\n사전점검 결과를 남깁니다."],
    ["보완 요청 대응", "요청문을 항목별로 나누고 답변 초안과\n수정할 서류를 함께 확인합니다."],
    ["재제출과 결과", "보완 답변서를 첨부해 재제출합니다.\n외부 결과에 통지서를 연결합니다."],
  ]);
  await sample(slide, "revision.png", "외부 보완 요청과 항목별 답변 준비 화면");
  notes(slide, "§3.1, §3.5", "공용 과제의 ‘수정 후 신속심의’는 후속 심의를 기다리는 상태다. 결과통지서와 면제확인서는 실제 외부 문서를 업로드한다.", "revision.png");
}
{
  const slide = head("승인 후 변경·지속심의·보고", "주 신청 경로와 별개로 발생한 보고도 제출 이력에 남깁니다.");
  rows(slide, [
    ["주 신청 경로", "변경심의, 지속심의, 종료보고에 맞는 서류를 확인하고 결과를 기록합니다."],
    ["병행 보고", "위반·이탈, 이상반응, 조기종료, 이해상충 변경,\n정보·검체 제공과 폐기·이관을 별도 기록합니다."],
    ["이미 시행한 변경", "변경 준비 단계에서 시행 여부를 확인하고 이탈 보고로 연결합니다."],
    ["만료·기한 알림", "승인 만료 60·30·7일 전에 알립니다. 이해상충 변경은\n인지일로부터 30일을 기준으로 안내합니다."],
  ], { split: 247, step: 97 });
  notes(slide, "§3.1, §3.13, §8", "이탈 및 이상반응은 발생일과 기한 확인 알림을 지원한다. 구체적인 기한 일수는 적용 SOP를 확인해야 한다.");
}
{
  const slide = head("기관 서식과 웹 작성 양식", "병원마다 다른 서식과 추가 입력 항목을 관리자 화면에서 관리합니다.");
  stacked(slide, [
    ["DOCX·HWPX 서식", "자리표시자를 과제 항목과 연결합니다.\n서류 종류와 변형별 현행판을 관리합니다."],
    ["웹 작성과 전자서명", "연구진이 이해상충 공개서와 서약서를\n작성·서명하면 서류함에 첨부됩니다."],
    ["기관별 설정", "과제번호 형식과 추가 신청 항목을\n정의하고 양식 팩을 가져올 수 있습니다."],
  ]);
  await sample(slide, "templates.png", "기관 서식 파일 업로드 및 서류 종류별 버전 관리 화면");
  notes(slide, "§3.8, §3.9", "구형 HWP는 한글에서 HWPX로 저장 후 서식으로 등록한다. 서명 위치처럼 직접 작성할 곳은 자리표시자를 보존하도록 지정할 수 있다.", "templates.png");
}
{
  const slide = head("공용 IRB 요구표와 권고서식", "과제에서 공용 IRB를 선택하면 신청 조건에 맞는 요구표와 서식을 보여 줍니다.");
  rows(slide, [
    ["과제별 신청 조건", "기관협약·개인연구자·기관장 합의 경로와 연구 유형, 동의 방식을 등록합니다."],
    ["공통 제출 요구표", "사전점검, 패키지, QC, 제출 이력과 API가 같은 기준표를 사용합니다."],
    ["권고서식과 원본", "연구 방법에 따라 권고서식을 안내하고, 검수된 원본 및 판본을 관리합니다."],
    ["평가와 이력", "연구자 자가점검과 사전검토자 판정을 기록합니다.\n제출 당시 기준과 첨부·점검표 정보를 저장합니다."],
  ], { split: 260, step: 95 });
  notes(slide, "§3.13, §8", "e-IRB 입력 항목은 준비됨, 제출함, 해당 없음으로 기록한다. 검수된 생성용 서식이 없으면 원본이나 공용위원회 자료실로 안내한다. 검수된 기관협약 목록이 있으면 소속기관을 조회할 수 있다.");
}
{
  const slide = head("규정 검색과 의료데이터 도구", "연구 설계와 신청 근거 마련에 필요한 자료를 확인할 수 있습니다.");
  rows(slide, [
    ["규정·사례 검색", "법령, SOP, FAQ와 열람 가능한 과거 보완 사례를 검색합니다.\n사전점검 의견에서 관련 근거로 이동할 수 있습니다."],
    ["대상자 수 사전 조회", "CDW 또는 합성 데이터에서 집계를 조회합니다.\n작은 수는 가리고 조회 한도와 이력을 관리합니다."],
    ["연구용 CSV 가명처리", "식별자 탐지, 가명·마스킹·범주화 등을 적용하고\nk-익명성과 l-다양성을 평가해 보고서를 만듭니다."],
  ], { split: 294, step: 128 });
  notes(slide, "§3.10–3.12", "규정·사례 검색은 한글 2글자 단위 색인 및 BM25를 사용한다. 가명처리 키는 세션 키와 분리하며 원본 의료데이터는 AI로 전송하지 않는다.");
}
{
  const slide = head("AI 사용 범위와 개인정보 보호", "기본값은 AI 꺼짐입니다. 문서 처리에 AI를 쓰는 경우 전송 기준을 적용합니다.");
  rows(slide, [
    ["보조 작업", "Claude를 켜면 초안 작성, 심층 점검, OCR, 추출·요약을 보조합니다."],
    ["텍스트 전송", "이름·주민번호·전화번호·이메일·주소 등 개인정보 패턴을 마스킹합니다."],
    ["원본 파일 전송", "허용된 서류 종류이고 개인정보가 탐지되지 않을 때만 PDF·이미지를 보냅니다."],
    ["감사 이력", "전송 목적, 파일 해시, 크기, 마스킹 건수를 기록합니다.\nAI 호출에 실패하면 규칙 경로로 가능한 작업을 계속합니다."],
  ], { split: 230, step: 98 });
  notes(slide, "§3.14, §4", "AI 제안은 참고용이다. 서비스는 세션과 CSRF 보호를 적용하고 상태 변경, 문서 열람·반출, AI 전송 이력을 기록한다.");
}
{
  const slide = head("기술 구성과 데이터 관리", "웹 화면, 업무 규칙, 문서 처리 모듈이 과제와 제출 이력을 공유합니다.");
  rows(slide, [
    ["서비스 구성", "FastAPI, Jinja2, SQLAlchemy로 웹과 REST API를 제공합니다."],
    ["문서 처리", "자체 파서와 pypdf로 본문을 읽고 LibreOffice로 문서를 변환합니다."],
    ["과제 데이터", "과제에 제출 이력, 문서 버전, 보완 항목과 QC 실행 결과를 연결합니다."],
    ["배포와 복원", "Docker로 실행합니다. 기본 DB는 SQLite입니다. 파일과 키를 포함한\n데이터 볼륨을 같은 시점 기준으로 백업하고 복원합니다."],
  ], { split: 249, step: 95 });
  notes(slide, "§5–7", "주요 모듈은 workflow, engine, package, qc, submission, docproc, pipeline, doctemplates, forms, public_irb, knowledge, deid 및 web/api다. 요구표 API는 /api/protocols/{id}/requirements다. 기관 설정 파일은 rules/default_ruleset.yaml, qc/criteria.yaml, qc/rules.yaml, qc/required_docs.yaml이다.");
}
{
  const slide = head("운영 전 확인 사항", "기관 기준, 실제 공용 서식과 배포 환경을 점검합니다.", true);
  rows(slide, [
    ["기관 기준 반영", "사전점검 규칙과 QC 기준을 기관 SOP에 맞춥니다."],
    ["공용 IRB 원문 검수", "원본 등록, 현행판·평가 문항 검수와 HWPX 파생본 대조가 남아 있습니다.\n98개 참고 시나리오 중 핵심 11개를 자동 검증했습니다."],
    ["보고 기한과 문서 인식", "이탈·이상반응의 기한은 적용 SOP에서 확인합니다.\n스캔본은 AI를 활성화해야 OCR을 사용할 수 있습니다."],
    ["배포 환경 점검", "HTTPS·원내망 제한, 비밀 키, 운영 DB와 백업을 확인합니다.\nAI 사용 시 기관 승인을 확인합니다."],
  ], { split: 286, step: 106, dark: true });
  notes(slide, "§8–9", "세부 안내는 기능_및_화면_안내.md, 운영가이드.md, 서류점검.md, AI_문서자동화_파이프라인.md, 기관서식_템플릿.md, 기관양식.md, 관련정보검색.md, 웹문서제출.md 및 공용IRB_통합_구현결과.md 참조.");
}

const candidate = path.join(build, "candidate.pptx");
await (await PresentationFile.exportPptx(presentation)).save(candidate);
const renderDir = path.join(build, "render");
await fs.mkdir(renderDir, { recursive: true });
for (const [i, slide] of presentation.slides.items.entries()) {
  const png = await presentation.export({ slide, format: "png", scale: 1 });
  await fs.writeFile(path.join(renderDir, String(i + 1).padStart(2, "0") + ".png"),
    new Uint8Array(await png.arrayBuffer()));
}
const { finalizePresentation } = await import(pathToFileURL(
  path.join(skill, "container_tools/artifact_tool_utils.mjs")).href);
const finalPath = path.join(root, "docs", "서비스_전체_정리.pptx");
const result = await finalizePresentation({
  workspaceDir: root, candidatePath: candidate, finalPath,
  pythonExecutable: path.join(runtime, "python/python.exe"),
  integrityValidatorPath: path.join(skill, "container_tools/inspect_presentation_package_integrity.py"),
  layoutValidatorPath: path.join(skill, "container_tools/inspect_presentation_layout_geometry.py"),
  layoutArgs: ["--expected-slide-size-emu", "12192000,6858000", "--validate-heading-fit"],
  fontPolicy: { basis: "design", families: [font] },
  verifyArtifactToolImport: true,
  receiptPath: path.join(build, "validation.json"),
}).catch(async (error) => {
  if (error.code !== "EPERM" || error.syscall !== "link" || error.dest !== finalPath) throw error;
  // The checks finish before the finalizer publishes through a hard link.
  await fs.copyFile(candidate, finalPath, constants.COPYFILE_EXCL);
  if (!(await fs.readFile(candidate)).equals(await fs.readFile(finalPath))) throw error;
  return { finalPath, windowsPublish: "copied verified bytes" };
});
console.log(JSON.stringify(result));
