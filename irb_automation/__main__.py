"""명령행 도구.

  python -m irb_automation init-db [--demo]       DB 생성 (데모 사용자/과제 포함)
  python -m irb_automation create-user USER ROLE  사용자 생성
  python -m irb_automation run-scheduler          만료 처리·알림 (cron 등록용)
  python -m irb_automation verify-audit           감사 로그 해시 체인 검증
  python -m irb_automation serve [--host --port]  웹 서버 실행
"""
from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select


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
                from .seed import DEMO_PASSWORD, seed_demo

                users = seed_demo(db)
                db.commit()
                print("데모 데이터 생성 완료. 공통 비밀번호:", DEMO_PASSWORD)
                for u in users.values():
                    print(f"  {u.username:12s} {u.role.label}")
            else:
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
        elif args.cmd == "verify-audit":
            from .audit import verify_chain

            ok, broken = verify_chain(db)
            print("정상" if ok else f"위변조 의심: 감사로그 #{broken}")
            return 0 if ok else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
