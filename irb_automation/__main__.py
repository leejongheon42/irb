"""명령행 도구.

  python -m irb_automation init-db [--demo]       DB 생성 (데모 사용자/과제 포함)
  python -m irb_automation create-user USER ROLE  사용자 생성
  python -m irb_automation run-scheduler          만료 처리·알림 (cron 등록용)
  python -m irb_automation verify-audit           감사 로그 해시 체인 검증
  python -m irb_automation serve [--host --port]  웹 서버 실행
  python -m irb_automation demo-pipeline         샘플로 전체 파이프라인 테스트(단계별 결과 출력)
"""
from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select


def seed_extensions(db, demo: bool = False) -> None:
    """기관 양식 팩·지식베이스·제출 설정 기본값 적재(멱등). 데모 서식은 --demo에서만."""
    from .forms.packs import seed_forms
    from .knowledge.sources import reindex_protocols, seed_knowledge
    from .submission.seed import seed_submission

    seed_forms(db)
    seed_submission(db)
    seed_knowledge(db)
    reindex_protocols(db)
    if demo:
        from .doctemplates.samples import seed_doctemplates

        seed_doctemplates(db)
        db.commit()
        from .pipeline.service import seed_pipeline

        seed_pipeline(db)
    db.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="irb_automation")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init-db")
    p_init.add_argument("--demo", action="store_true", help="데모 사용자·과제 생성")
    p_user = sub.add_parser("create-user")
    p_user.add_argument("username")
    p_user.add_argument("role", choices=["researcher", "coordinator", "reviewer", "chair", "data_steward", "admin"])
    p_user.add_argument("--name", default="")
    sub.add_parser("run-scheduler")
    sub.add_parser("verify-audit")
    p_demo = sub.add_parser("demo-pipeline", help="샘플로 전체 파이프라인을 실행하고 단계별 결과 출력")
    p_demo.add_argument("--user", default="researcher")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    from .database import SessionLocal, init_db

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("irb_automation.main:app", host=args.host, port=args.port)
        return 0

    init_db()
    with SessionLocal() as db:
        if args.cmd == "init-db":
            if args.demo:
                from .seed import DEMO_PASSWORD, seed_demo, seed_rich_demo

                users = seed_demo(db)
                db.commit()
                seed_rich_demo(db)
                db.commit()
                seed_extensions(db, demo=True)
                print("데모 데이터 생성 완료. 비밀번호는 아이디와 같습니다:")
                for u in users.values():
                    print(f"  {u.username:12s} / {DEMO_PASSWORD or u.username:12s} {u.role.label}")
            else:
                seed_extensions(db)
                print("DB 초기화 완료")
        elif args.cmd == "create-user":
            from .models import Role, User
            from .security import hash_password, password_problems

            if db.scalars(select(User).where(User.username == args.username)).first():
                print("이미 존재하는 아이디입니다.", file=sys.stderr)
                return 1
            pw = getpass.getpass("비밀번호: ")
            problems = password_problems(pw)
            if problems:
                print("비밀번호 정책 미충족:", ", ".join(problems), file=sys.stderr)
                return 1
            db.add(User(username=args.username, password_hash=hash_password(pw),
                        full_name=args.name or args.username, role=Role(args.role)))
            db.commit()
            print("생성 완료")
        elif args.cmd == "run-scheduler":
            from .workflow.service import run_scheduler

            stats = run_scheduler(db)
            db.commit()
            print(stats)
        elif args.cmd == "demo-pipeline":
            from .demo.full import run_full_pipeline
            from .models import User

            u = db.query(User).filter_by(username=args.user).first()
            if u is None:
                print(f"사용자 {args.user}가 없습니다(init-db --demo 먼저 실행).")
                return 1
            d = run_full_pipeline(db, u)
            for s_ in d.steps:
                print(f"{s_['n']:>2}. [{s_['status']:7s}] {s_['title']} - {s_['message']}")
            print("결과:", d.status, d.summary)
            return 0 if d.status != "failed" else 2
        elif args.cmd == "verify-audit":
            from .audit import verify_chain

            ok, broken = verify_chain(db)
            print("정상" if ok else f"위변조 의심: 감사로그 #{broken}")
            return 0 if ok else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
