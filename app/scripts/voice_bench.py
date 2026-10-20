"""
Measures voice latency end to end against a running API (P7 plan 2.11):
plays recorded question WAVs through the real socket in real time and
prints p50/p95 per stage, so the CLAUDE.md §15 targets can be checked with
the providers actually configured (Copilot: first visible words <= 1 s p50;
Practice: first spoken word <= 1.5 s p50).

Recordings: 16 kHz mono 16-bit WAV files, one question each (record a few
yourself - they are test inputs, never user audio). The access token is
read from the VOICE_BENCH_TOKEN environment variable (not the command line),
and the account needs the feature (copilot: live_copilot + acknowledgement).

Run from job_finder_api/:
    set VOICE_BENCH_TOKEN=<access token>
    python -m app.scripts.voice_bench --base-url http://127.0.0.1:8000 --mode copilot --wavs bench_wavs/
    python -m app.scripts.voice_bench --base-url http://127.0.0.1:8000 --mode mock --wavs answers/
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import wave
from pathlib import Path

import httpx

FRAME_MS = 40


def _pct(values: list[float]) -> str:
    if not values:
        return "n/a"
    values = sorted(values)
    p95 = values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]
    return f"p50 {statistics.median(values):.0f} ms | p95 {p95:.0f} ms | n={len(values)}"


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path.name}: needs 16 kHz mono 16-bit PCM")
        return w.readframes(w.getnframes())


async def _play(ws, channel: int, pcm: bytes, clock: list[int], silence_ms: int = 1500) -> None:
    step = FRAME_MS * 32
    for i in range(0, len(pcm) + silence_ms * 32, step):
        chunk = pcm[i:i + step] if i < len(pcm) else b"\0" * step
        chunk = chunk.ljust(step, b"\0")
        await ws.send(bytes([channel]) + clock[0].to_bytes(4, "little") + chunk)
        clock[0] += FRAME_MS
        await asyncio.sleep(FRAME_MS / 1000)


async def bench(base_url: str, token: str, mode: str, wavs: list[Path]) -> dict:
    from websockets.asyncio.client import connect

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as http:
        resp = await http.post("/api/v1/voice/sessions", json={"mode": mode, "length": len(wavs)})
        if resp.status_code != 201:
            raise SystemExit(f"could not start a session: {resp.status_code} {resp.text[:300]}")
        created = resp.json()
    ws_url = base_url.replace("http", "ws", 1) + created["stream_path"]
    stats: dict[str, list[float]] = {"first_visible_ms": [], "server_first_token_ms": [], "server_stt_ms": []}
    channel = 0 if mode == "copilot" else 1
    clock = [0]
    async with connect(ws_url, max_size=2 ** 22) as ws:
        await ws.send(json.dumps({"type": "auth", "token": created["stream_token"]}))
        print(json.loads(await ws.recv()).get("providers"))
        if mode == "mock":
            await ws.send(json.dumps({"type": "control", "action": "start"}))
        for path in wavs:
            if mode == "mock":  # wait for the interviewer's question before answering
                while json.loads(await ws.recv()).get("type") != "turn.done":
                    pass
            await _play(ws, channel, _read_wav(path), clock, silence_ms=0)
            spoken_end = time.monotonic()
            await _play(ws, channel, b"", clock, silence_ms=200)
            first = None
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                except asyncio.TimeoutError:
                    print(f"{path.name}: no answer within 15 s")
                    break
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind in ("answer.delta", "interviewer.delta") and first is None:
                    first = (time.monotonic() - spoken_end) * 1000
                    stats["first_visible_ms"].append(first)
                if kind in ("answer.done", "turn.done"):
                    lat = msg.get("latency") or {}
                    if lat.get("first_token_ms") is not None:
                        stats["server_first_token_ms"].append(lat["first_token_ms"])
                    if lat.get("stt_ms") is not None:
                        stats["server_stt_ms"].append(lat["stt_ms"])
                    print(f"{path.name}: first words after {first or 0:.0f} ms")
                    break
                if kind == "error":
                    print(f"{path.name}: error {msg.get('code')}: {msg.get('message')}")
        await ws.send(json.dumps({"type": "control", "action": "stop" if mode == "copilot" else "end"}))
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--mode", choices=("copilot", "mock"), default="copilot")
    parser.add_argument("--wavs", required=True, help="folder of 16 kHz mono WAV files")
    args = parser.parse_args()
    token = os.environ.get("VOICE_BENCH_TOKEN", "")
    if not token:
        sys.exit("Set VOICE_BENCH_TOKEN to an access token first.")
    wavs = sorted(Path(args.wavs).glob("*.wav"))
    if not wavs:
        sys.exit(f"No .wav files in {args.wavs}")
    stats = asyncio.run(bench(args.base_url.rstrip("/"), token, args.mode, wavs))
    target = 1000 if args.mode == "copilot" else 1500
    print(f"\nfirst visible/spoken words (client-measured, target p50 <= {target} ms): "
          f"{_pct(stats['first_visible_ms'])}")
    print(f"server first token: {_pct(stats['server_first_token_ms'])}")
    if stats["server_stt_ms"]:
        print(f"server speech-to-text: {_pct(stats['server_stt_ms'])}")


if __name__ == "__main__":
    main()
