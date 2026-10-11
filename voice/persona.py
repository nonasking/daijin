#!/usr/bin/env python3
# persona — 다이진의 인격 글을 다룬다. 밤 반성이 제안하고 사람이 승인하는 한 군데 (docs/design-self-layer.md §8).
#   show              현재 글과 승인 기다리는 제안들 (제안은 현재 글과의 차이로)
#   approve <id>      제안을 현재로 (이전 현재는 superseded)
#   reject <id>
#   export [파일]     현재 글을 파일로 (기본 voice/persona.local.md) — 사람이 편집하는 자리
#   import [파일]     파일을 현재 글로 (사람이 직접 고친 것 = 제안과 승인을 한 번에)
#   seed              현재 글이 없으면 voice/props.local.txt(없으면 props.txt)로 첫 버전을 만든다
import difflib, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daijin_common import VOICE
import selfdb

DEFAULT_FILE = os.path.join(VOICE, "persona.local.md")

def _diff(old, new):
    return "".join(difflib.unified_diff((old or "").splitlines(True), new.splitlines(True), "현재", "제안", n=1))

def show():
    cur = selfdb.persona_current()
    print("=== 현재" + (f" (#{cur['id']}, {time.strftime('%Y-%m-%d', time.localtime(cur['ts']))}, {cur['proposed_by']})" if cur else " (없음)"))
    if cur:
        print(cur["text"])
    for p in selfdb.persona_pending():
        print(f"\n=== 제안 #{p['id']} ({time.strftime('%Y-%m-%d %H:%M', time.localtime(p['ts']))}, {p['proposed_by']}): {p['diff_summary']}")
        print(_diff(cur["text"] if cur else "", p["text"]) or "(차이 없음)")
    if not selfdb.persona_pending():
        print("\n승인 기다리는 제안 없음")

def main(argv):
    if not argv or argv[0] == "show":
        show(); return 0
    cmd = argv[0]
    if cmd in ("approve", "reject"):
        if len(argv) < 2 or not argv[1].isdigit():
            print("사용법: persona.py approve|reject <id>"); return 2
        ok = selfdb.persona_approve(int(argv[1])) if cmd == "approve" else selfdb.persona_reject(int(argv[1]))
        print(("승인됨" if cmd == "approve" else "거절됨") if ok else "실패: 그 id 의 제안이 없다")
        return 0 if ok else 1
    if cmd == "export":
        cur = selfdb.persona_current()
        if not cur:
            print("현재 글이 없다 (seed 먼저)"); return 1
        path = argv[1] if len(argv) > 1 else DEFAULT_FILE
        open(path, "w", encoding="utf-8").write(cur["text"].rstrip("\n") + "\n"); print(f"내보냄: {path}"); return 0
    if cmd == "import":
        path = argv[1] if len(argv) > 1 else DEFAULT_FILE
        text = open(path, encoding="utf-8").read().strip()
        cur = selfdb.persona_current()
        if cur and cur["text"].strip() == text:
            print("현재 글과 같다"); return 0
        vid = selfdb.persona_set_current(text, by="human", diff_summary=f"{os.path.basename(path)} 에서 가져옴")
        print(f"현재로 설정됨 (#{vid})"); return 0
    if cmd == "seed":
        if selfdb.persona_current():
            print("이미 현재 글이 있다"); return 0
        for name in ("props.local.txt", "props.txt"):
            p = os.path.join(VOICE, name)
            if os.path.exists(p):
                vid = selfdb.persona_set_current(open(p, encoding="utf-8").read().strip(), by="human", diff_summary=f"{name} 로 첫 버전")
                print(f"첫 버전 #{vid} ← {name}"); return 0
        print("props 파일이 없다"); return 1
    print(__doc__ or "show | approve <id> | reject <id> | export [file] | import [file] | seed"); return 2

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
