#!/usr/bin/env bash
# GitHub Codespaces 시연용 서버 기동. 데모 계정 비밀번호 = 아이디 (researcher/researcher, admin/admin).
# 실제 환자 정보는 절대 넣지 마십시오(공개 포트는 주소를 아는 누구나 접속 가능).
set -euo pipefail
cd "$(dirname "$0")/.."
export IRB_DATA_DIR="${IRB_DATA_DIR:-$PWD/data}"
export IRB_SESSION_HTTPS_ONLY=true
export IRB_SCHEDULER_ENABLED=false
mkdir -p "$IRB_DATA_DIR"

# 이전 버전(위원회 기능 포함) 데이터는 호환되지 않으므로 새로 만든다
if [ ! -f "$IRB_DATA_DIR/.v4" ]; then
  rm -rf "${IRB_DATA_DIR:?}"/* && touch "$IRB_DATA_DIR/.v4"
fi
# 세션 키를 파일로 고정: 서버를 다시 띄워도 로그인·CSRF 토큰이 깨지지 않게
[ -f "$IRB_DATA_DIR/.secret_key" ] || openssl rand -hex 32 > "$IRB_DATA_DIR/.secret_key"
[ -f "$IRB_DATA_DIR/.pseudonym_key" ] || openssl rand -hex 32 > "$IRB_DATA_DIR/.pseudonym_key"
export IRB_SECRET_KEY="$(cat "$IRB_DATA_DIR/.secret_key")"
export IRB_PSEUDONYM_SECRET="$(cat "$IRB_DATA_DIR/.pseudonym_key")"

[ -f "$IRB_DATA_DIR/irb.db" ] || python -m irb_automation init-db --demo >/dev/null
# 데모 계정 비밀번호를 아이디와 같게 맞춘다
python - <<'PY'
from irb_automation.database import SessionLocal
from irb_automation.models import User
from irb_automation.security import hash_password
with SessionLocal() as db:
    for u in db.query(User).filter(User.username.in_(["researcher", "researcher2", "admin"])):
        u.password_hash = hash_password(u.username)
    db.commit()
PY

pkill -f "uvicorn irb_automation.main:app" 2>/dev/null || true
nohup python -m uvicorn irb_automation.main:app --host 0.0.0.0 --port 8000 --proxy-headers \
  --forwarded-allow-ips='*' > "$IRB_DATA_DIR/server.log" 2>&1 &
sleep 3

if [ -n "${CODESPACE_NAME:-}" ]; then
  gh codespace ports visibility 8000:public -c "$CODESPACE_NAME" >/dev/null 2>&1 || true
  echo "접속 주소: https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
fi
echo "로그인: researcher / researcher · researcher2 / researcher2 · admin / admin"
