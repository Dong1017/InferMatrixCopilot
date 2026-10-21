import asyncio
import json
import subprocess
import sys
from pathlib import Path

import edge_tts
from aiohttp import ClientConnectionError

GAP = 0.35        # pause between lines (seconds)
LEAD = 0.5        # silence at the start of each scene
TAIL = 0.6        # silence at the end of each scene
SYNTH_TIMEOUT = 45
MAX_ATTEMPTS = 3


def duration(path: Path) -> float:
    # read audio duration with ffprobe
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def validate_audio(path: Path) -> None:
    # 完整解码，避免损坏音频进入缓存或时间轴。
    subprocess.run(
        ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    )


async def synth(text: str, voice: str, rate: str, mp3: Path) -> None:
    part = mp3.with_suffix(".part.mp3")
    try:
        for attempt in range(MAX_ATTEMPTS):
            try:
                await asyncio.wait_for(
                    edge_tts.Communicate(text, voice, rate=rate).save(str(part)),
                    timeout=SYNTH_TIMEOUT,
                )
                break
            except (edge_tts.exceptions.NoAudioReceived, ClientConnectionError, asyncio.TimeoutError) as error:
                part.unlink(missing_ok=True)
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                print(f"TTS {type(error).__name__}: retry {attempt + 1}/{MAX_ATTEMPTS - 1}", flush=True)
                await asyncio.sleep(3 + attempt * 3)
        validate_audio(part)
        part.replace(mp3)
    finally:
        # 失败或取消时清理半成品，成功后只保留最终音频。
        part.unlink(missing_ok=True)
    await asyncio.sleep(1.5)


async def main(root: Path) -> None:
    spec = json.loads((root / "narration.json").read_text(encoding="utf-8"))
    audio = root / "build" / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    t = 0.0
    scenes = []
    for scene in spec["scenes"]:
        start = t
        cursor = start + LEAD
        lines = []
        for i, line in enumerate(scene["lines"]):
            mp3 = audio / f"{scene['id']}_{i}.mp3"
            # reuse existing audio; delete the mp3 after changing its "say" text
            if not mp3.exists() or mp3.stat().st_size == 0:
                await synth(line["say"], spec["voice"], spec["rate"], mp3)
            else:
                validate_audio(mp3)
            d = duration(mp3)
            lines.append({"file": str(mp3), "start": round(cursor, 3), "dur": round(d, 3), "cap": line["cap"]})
            cursor += d + GAP
        # scene length is the longer of narration time and the animation minimum
        length = max(cursor - GAP + TAIL - start, scene["min_len"])
        scenes.append({"id": scene["id"], "start": round(start, 3), "len": round(length, 3), "lines": lines})
        t = start + length
    timeline = {"total": round(t, 3), "scenes": scenes}
    (root / "build" / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"total={t:.2f}s")
    for s in scenes:
        print(s["id"], s["start"], s["len"])


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
