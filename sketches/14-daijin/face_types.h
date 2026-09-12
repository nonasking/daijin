// 얼굴 상태 enum — .ino 본문에 두면 Arduino 프로토타입 자동 생성이 enum보다 앞에 끼어들어 컴파일 에러가 난다.
#pragma once
enum Face { F_IDLE, F_LISTEN, F_UPLOAD, F_THINK, F_SPEAK, F_ERROR };
