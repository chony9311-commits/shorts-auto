"""테스트 공용: 실제 ffmpeg로 가짜 소스 영상/음성을 만든다."""
import subprocess
from pathlib import Path

from app.render import ffmpeg_exe


def make_video(path: Path, w=1280, h=720, sec=8.0, border=False, audio=False) -> Path:
    vf = "format=yuv420p"
    # border=True: 파랑 바탕 + 빨강 테두리(크롭 검증용으로 가장자리 색이 확실히 구분된다)
    src = f"testsrc2=size={w}x{h}:rate=30:duration={sec}"
    if border:
        src = f"color=c=blue:size={w}x{h}:rate=30:duration={sec}"
        vf = f"drawbox=x=0:y=0:w={w}:h={h}:color=red:t=24,format=yuv420p"
    cmd = [ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", src]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=330:duration={sec}"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True)
    return path


class FakeTts:
    """edge-tts 대신: 글자 수에 비례한 사인파 + 앞 0.4초/뒤 0.6초 '숨'(무음)을 붙여 mp3 로 만든다."""
    SEC_PER_CHAR = 0.14      # 초당 약 7글자(+18% 한국어 TTS 대략치)

    def __init__(self):
        self.calls = []

    async def synth(self, text, voice, rate, out_path):
        from app.script_gen import visible_len
        self.calls.append((text, voice, rate))
        d = max(0.5, visible_len(text) * self.SEC_PER_CHAR)
        subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i",
                        f"sine=frequency=440:duration={d}", "-af", "adelay=400|400,apad=pad_dur=0.6",
                        "-c:a", "libmp3lame", str(out_path)], check=True)
