import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from irb_automation.config import Settings, override_settings
override_settings(Settings(data_dir=ROOT/'.omo/qa-ui/data', database_url='sqlite:///'+str(ROOT/'.omo/qa-ui/data/qa.db'), secret_key='local-qa-only', pseudonym_secret='local-qa-only', scheduler_enabled=False, builtin_accounts=False, auto_login_user='qa', ai_review_enabled=False))
from irb_automation.database import init_db, SessionLocal
init_db()
from irb_automation.models import User, Role
from irb_automation.qc.models import QCRun
from irb_automation.qc.category_scores import score_run
from reportlab.pdfgen import canvas
from irb_automation.mvp.evidence import findings_evidence
with SessionLocal() as db:
    user=db.get(User,999)
    if not user:
        user=User(id=999,username='qa',full_name='QA 검토자',role=Role.RESEARCHER,password_hash='disabled')
        db.add(user)
        db.commit()
    if not db.get(QCRun,999):
        path=ROOT/'.omo/qa-ui/data/qa.pdf'
        c=canvas.Canvas(str(path))
        c.drawString(72,720,'Sample size: 100 subjects. Retention: 3 years.')
        c.showPage();c.drawString(72,720,'Consent is voluntary.');c.save()
        items=[('critical','required','개인정보 보호 대책 누락'),('major','cross','대상자 수 불일치'),('minor','typo','용어 표기 오탈자'),('major','required','동의 면제 조건 확인')]
        issues=[dict(id=i+1,severity=sev,category=cat,title=title,detail='가상 연구계획서의 제출 내용을 확인합니다.',suggestion='원문을 확인하고 해당 항목을 수정해 주세요.',doc='qa',doc_name='검증용 연구계획서.pdf',source='rule',evidence='Sample size: 100 subjects.',result='needs_check' if i==3 else 'mismatch') for i,(sev,cat,title) in enumerate(items)]
        run=QCRun(id=999,created_by=999,mvp_attempt='local-qa',status='done',docs=[dict(key='qa',name='검증용 연구계획서.pdf',path=str(path),fmt='pdf',chars=100,label='연구계획서')],issues=issues,phases={'2':{'status':'skipped'}},criteria_version='QA')
        score_run(run)
        run.counts={**run.counts,'source_locations':findings_evidence(run)}
        db.add(run);db.commit()
from irb_automation.main import app
import uvicorn
if '--before' in sys.argv:
    from jinja2 import ChoiceLoader, FileSystemLoader
    from irb_automation.web.deps import templates
    from fastapi.staticfiles import StaticFiles
    import shutil
    base=ROOT/'.omo/qa-ui/before'
    (base/'templates/mvp').mkdir(exist_ok=True)
    for src in (base/'templates').glob('*.html'):
        shutil.copy2(src,base/'templates/mvp'/src.name)
    templates.env.loader=ChoiceLoader([FileSystemLoader(str(base/'templates')),templates.env.loader])
    for src in (ROOT/'irb_automation/web/static').iterdir():
        if src.is_file() and not (base/src.name).exists(): shutil.copy2(src,base/src.name)
    app.routes.insert(0,__import__('starlette.routing',fromlist=['Mount']).Mount('/static',app=StaticFiles(directory=base)))
uvicorn.run(app,host='127.0.0.1',port=8766 if '--before' in sys.argv else 8765)
