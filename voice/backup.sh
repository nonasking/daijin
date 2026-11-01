#!/bin/bash
# 서버(다이진 브레인)의 작고 되살릴 수 없는 것들을 이 기계(맥)로 매일 끌어와 암호화해 둔다.
#   secrets.local.txt · voice/.daijin_session(.prev) · voice/daijin.db(일관된 복사) · ~/.claude(브레인의 클로드 세션 기록) · systemd 유닛
# 서버→맥이 아니라 맥→서버로 끌어온다: 맥은 자주 잠들고 주소가 바뀌지만 서버는 늘 있고, offline-mode.sh 가 이미 같은 ssh 길을 쓴다.
# 암호: gpg 대칭(AES256). 열쇠는 macOS 키체인(daijin-backup)에만 있어서 백업 폴더가 어디로 복사돼도 열쇠는 따라가지 않는다.
#       첫 실행이 열쇠를 만들어 한 번만 보여 준다: 비밀번호 관리자에 적어 둘 것. 맥을 잃으면 그 열쇠로만 백업을 연다.
#
# 사용:  bash voice/backup.sh                 지금 한 번 → ~/.daijin-backup/daijin-YYYY-MM-DD.tar.gz.gpg (최근 14개 보관)
#        bash voice/backup.sh daily           오늘 것이 없을 때만 한 번 (launchd 가 한 시간마다 부른다)
#        bash voice/backup.sh install         launchd(com.daijin.backup) 등록: 한 시간마다 daily
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

passphrase() {          # 키체인의 열쇠. 없으면 사람이 터미널 앞에 있을 때만 만든다 ($1=new 일 때)
  local p
  if ! p="$(security find-generic-password -s "$KEY" -a "$USER" -w 2>/dev/null)"; then
    # launchd 로 돌 때 만들면 열쇠가 stderr 로, 곧 백업과 같은 폴더의 backup.log 로 간다. restore 에서 만들면 옛 백업을 못 여는
    # 새 열쇠가 생긴다 (새 맥에서 복구할 때가 바로 그 경우다). 그래서 둘 다 여기서 멈춘다.
    [ "$1" = new ] && [ -t 2 ] || { echo "키체인에 $KEY 열쇠가 없다. 터미널에서 'bash voice/backup.sh' 로 처음 한 번 만들거나," \
      "복구라면 비밀번호 관리자에 적어 둔 열쇠를: security add-generic-password -s $KEY -a \$USER -w '<열쇠>'" >&2; exit 1; }
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
  pass="$(passphrase new)"
  # 임시 이름에 쓰고 검증이 끝난 뒤에만 오늘 이름으로 옮긴다. 바로 쓰면 ssh 가 실패해도 gpg 가 빈 입력을 암호화한 파일이 남아
  # daily 가 "오늘 것 있음"으로 보고 그날 남은 시도를 전부 건너뛰고, 그 빈 백업이 보관 14개 중 하나를 차지했다.
  local tmp="$out.part"; trap 'rm -f "$tmp"' EXIT
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
  ' | gpg --batch --yes --quiet --symmetric --cipher-algo AES256 --passphrase-fd 3 -o "$tmp" 3<<<"$pass"
  # 바로 열어 본다: 열쇠가 맞고 tar 가 멀쩡한지. 세는 것은 항목 수. 하나도 없으면 실패다.
  n="$(gpg --batch --quiet --decrypt --passphrase-fd 3 "$tmp" 3<<<"$pass" | tar tzf - | wc -l | tr -d ' ')"
  [ "${n:-0}" -gt 0 ] || { log "실패: 서버에서 아무것도 오지 않았다 (다음 시간에 다시)"; exit 1; }
  mv -f "$tmp" "$out"; trap - EXIT
  log "백업 $(basename "$out"): $n 항목, $(du -h "$out" | cut -f1)"
  ls -1t "$DEST"/daijin-*.tar.gz.gpg 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do rm -f "$f"; log "지움 $(basename "$f")"; done
}

case "${1:-run}" in
  run) run ;;
  daily) [ -e "$DEST/daijin-$(date +%F).tar.gz.gpg" ] || run ;;     # 오늘 것이 있으면 조용히 끝 (로그도 안 남긴다)
  list) ls -lh "$DEST"/daijin-*.tar.gz.gpg 2>/dev/null || echo "백업 없음 ($DEST)" ;;
  restore)
    [ -n "$2" ] || { echo "사용: backup.sh restore <파일> [폴더]"; exit 1; }
    dir="${3:-./restored}"; mkdir -p "$dir"
    gpg --batch --quiet --decrypt --passphrase-fd 3 "$2" 3<<<"$(passphrase)" | tar xzf - -C "$dir"     # 열쇠를 새로 만들지 않는다
    echo "풀었다: $dir  (서버에 올리는 절차는 이 파일 머리말)"; ls -la "$dir" ;;
  install)
    mkdir -p "$(dirname "$PLIST")" "$DEST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    sleep 2                                              # bootout 직후 bootstrap 은 EIO 로 실패할 때가 있다 (mac-helper.sh 와 같은 이유)
    sed "s|__REPO__|$REPO|g; s|__HOME__|$HOME|g; s|__DEST__|$DEST|g" "$VOICE/$LABEL.plist" > "$PLIST"
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "등록: $LABEL (한 시간마다, 하루 한 번만 백업) → $PLIST, 로그 $DEST/backup.log" ;;
  remove)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; rm -f "$PLIST"; echo "해제: $LABEL" ;;
  *) echo "사용: backup.sh [run|list|restore <파일> [폴더]|install|remove]"; exit 1 ;;
esac
