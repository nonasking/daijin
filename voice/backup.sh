#!/bin/bash
# 서버(다이진 브레인)의 작고 되살릴 수 없는 것들을 이 기계(맥)로 매일 끌어와 암호화해 둔다.
#   secrets.local.txt · voice/.daijin_session(.prev) · voice/daijin.db(일관된 복사) · ~/.claude(브레인의 클로드 세션 기록) · systemd 유닛
# 서버→맥이 아니라 맥→서버로 끌어온다: 맥은 자주 잠들고 주소가 바뀌지만 서버는 늘 있고, offline-mode.sh 가 이미 같은 ssh 길을 쓴다.
# 암호: gpg 대칭(AES256). 열쇠는 macOS 키체인(daijin-backup)에만 있어서 백업 폴더가 어디로 복사돼도 열쇠는 따라가지 않는다.
#       첫 실행이 열쇠를 만들어 한 번만 보여 준다: 비밀번호 관리자에 적어 둘 것. 맥을 잃으면 그 열쇠로만 백업을 연다.
#
# 사용:  bash voice/backup.sh                 지금 한 번 → ~/.daijin-backup/daijin-YYYY-MM-DD.tar.gz.gpg (최근 14개 보관)
#        bash voice/backup.sh install         매일 04:30 launchd(com.daijin.backup). 그 시각에 잠들어 있었으면 깨어난 뒤 돈다
#        bash voice/backup.sh remove          launchd 해제
#        bash voice/backup.sh list            보관 중인 백업과 크기
#        bash voice/backup.sh restore <파일> [폴더]    폴더(기본 ./restored)에 풀어만 둔다. 서버에 올리는 건 사람이 한다:
#   새 서버: 저장소 클론 → secrets.local.txt 를 저장소 루트에, voice/.daijin_session* 와 voice/daijin.db 를 voice/ 에,
#            .claude/ 를 ~/.claude 에, .config/systemd/user/ 를 그 자리에 → voice/install-services.sh
#            → 토큰이 만료됐으면 로그인된 기계에서 `claude setup-token`, secrets 의 CLAUDE_CODE_OAUTH_TOKEN·CLAUDE_TOKEN_EXPIRES 갱신.
set -e -o pipefail
VOICE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(dirname "$VOICE")"
sec() { grep "^$1=" "$REPO/secrets.local.txt" 2>/dev/null | cut -d= -f2-; }
DEST="${DAIJIN_BACKUP_DIR:-$HOME/.daijin-backup}"; KEEP=14
KEY=daijin-backup; LABEL=com.daijin.backup
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"     # launchd 에서도 gpg 를 찾게
log() { echo "[$(date '+%F %T')] $*"; }

passphrase() {
  local p
  if ! p="$(security find-generic-password -s "$KEY" -a "$USER" -w 2>/dev/null)"; then
    p="$(openssl rand -base64 36)"
    security add-generic-password -s "$KEY" -a "$USER" -w "$p" -U >/dev/null
    echo "새 백업 열쇠를 키체인($KEY)에 만들었다. 비밀번호 관리자에도 적어 둘 것 (이 줄은 다시 안 나온다):" >&2
    echo "  $p" >&2
  fi
  printf '%s' "$p"
}

run() {
  local from out pass n
  from="$(sec SELF_SYNC_FROM)"; [ -n "$from" ] || { echo "secrets 에 SELF_SYNC_FROM=user@host 가 필요하다"; exit 1; }
  mkdir -p "$DEST"; chmod 700 "$DEST"
  out="$DEST/daijin-$(date +%F).tar.gz.gpg"
  pass="$(passphrase)"
  # 서버에서: DB 는 sqlite 의 backup 으로 일관된 사본을 뜨고(브레인이 쓰는 중이어도 안전), 나머지는 그대로 tar 로 흘려보낸다.
  ssh -o BatchMode=yes -o ConnectTimeout=20 "$from" '
    set -e; cd "$HOME"
    T=$(mktemp -d); trap "rm -rf $T" EXIT
    mkdir -p "$T/esp32-iot/voice"
    cp esp32-iot/secrets.local.txt "$T/esp32-iot/" 2>/dev/null || true
    cp esp32-iot/voice/.daijin_session* "$T/esp32-iot/voice/" 2>/dev/null || true
    PY=python3; [ -x venv/bin/python3 ] && PY=venv/bin/python3
    "$PY" -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" \
          esp32-iot/voice/daijin.db "$T/esp32-iot/voice/daijin.db"
    tar czf - --exclude=.claude/downloads --exclude=.claude/shell-snapshots --exclude=.claude/session-env \
        -C "$T" esp32-iot -C "$HOME" .config/systemd/user .claude
  ' | gpg --batch --yes --quiet --symmetric --cipher-algo AES256 --passphrase-fd 3 -o "$out" 3<<<"$pass"
  [ -s "$out" ] || { rm -f "$out"; echo "빈 백업: 서버에서 아무것도 오지 않았다"; exit 1; }
  # 바로 열어 본다: 열쇠가 맞고 tar 가 멀쩡한지. 세는 것은 항목 수.
  n="$(gpg --batch --quiet --decrypt --passphrase-fd 3 "$out" 3<<<"$pass" | tar tzf - | wc -l | tr -d ' ')"
  log "백업 $(basename "$out"): $n 항목, $(du -h "$out" | cut -f1)"
  ls -1t "$DEST"/daijin-*.tar.gz.gpg 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do rm -f "$f"; log "지움 $(basename "$f")"; done
}

case "${1:-run}" in
  run) run ;;
  list) ls -lh "$DEST"/daijin-*.tar.gz.gpg 2>/dev/null || echo "백업 없음 ($DEST)" ;;
  restore)
    [ -n "$2" ] || { echo "사용: backup.sh restore <파일> [폴더]"; exit 1; }
    dir="${3:-./restored}"; mkdir -p "$dir"
    gpg --batch --quiet --decrypt --passphrase-fd 3 "$2" 3<<<"$(passphrase)" | tar xzf - -C "$dir"
    echo "풀었다: $dir  (서버에 올리는 절차는 이 파일 머리말)"; ls -la "$dir" ;;
  install)
    mkdir -p "$(dirname "$PLIST")" "$DEST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    sed "s|__REPO__|$REPO|g; s|__HOME__|$HOME|g; s|__DEST__|$DEST|g" "$VOICE/$LABEL.plist" > "$PLIST"
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "등록: $LABEL (매일 04:30) → $PLIST, 로그 $DEST/backup.log" ;;
  remove)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; rm -f "$PLIST"; echo "해제: $LABEL" ;;
  *) echo "사용: backup.sh [run|list|restore <파일> [폴더]|install|remove]"; exit 1 ;;
esac
