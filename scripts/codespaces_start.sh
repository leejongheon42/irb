#!/usr/bin/env bash
# GitHub Codespaces 시연용 서버 기동: 데모 데이터 생성 → 비밀번호 무작위 교체 → 서버 실행 → 포트 공개.
# 실제 환자 정보는 절대 넣지 마십시오(공개 포트는 주소를 아는 누구나 접속 가능).
set -euo pipefail
cd "$(dirname "$0")/.."
export IRB_DATA_DIR="${IRB_DATA_DIR:-$PWD/data}"
export IRB_SECRET_KEY="${IRB_SECRET_KEY:-$(openssl rand -hex 32)}"
export IRB_PSEUDONYM_SECRET="${IRB_PSEUDONYM_SECRET:-$(openssl rand -hex 32)}"
export IRB_SESSION_HTTPS_ONLY=true
export IRB_SCHEDULER_ENABLED=false
PW_FILE="$IRB_DATA_DIR/demo_password.txt"
mkdir -p "$IRB_DATA_DIR"

if [ ! -f "$PW_FILE" ]; then
  python -m irb_automation init-db --demo >/dev/null
  PW="Irb$(openssl rand -hex 5)!Q9"
  python - "$PW" <<'PY'
import sys
from irb_automation.database import SessionLocal
from irb_automation.models import User
from irb_automation.security import hash_password
with SessionLocal() as db:
    for u in db.query(User).all():
        u.password_hash = hash_password(sys.argv[1])
    db.commit()
PY
  echo "$PW" > "$PW_FILE"
fi

pkill -f "uvicorn irb_automation.main:app" 2>/dev/null || true
nohup python -m uvicorn irb_automation.main:app --host 0.0.0.0 --port 8000 --proxy-headers \
  --forwarded-allow-ips='*' > "$IRB_DATA_DIR/server.log" 2>&1 &
sleep 3

# 포트를 공개로 전환(실패하면 포트 탭에서 수동으로 Public 선택)
if [ -n "${CODESPACE_NAME:-}" ]; then
  gh codespace ports visibility 8000:public -c "$CODESPACE_NAME" >/dev/null 2>&1 || true
  echo "접속 주소: https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
fi
echo "데모 비밀번호: $(cat "$PW_FILE")  (아이디: researcher, coordinator, reviewer1, steward, admin)"
