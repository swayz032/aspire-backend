#!/usr/bin/env python3
"""Test ElevenLabs TTS for all 5 Aspire agent voices."""

import requests
import os

API_KEY = "sk_648c94797fbd0c1bb72a249d4b5b1d304978475395055e1b"
OUTPUT_DIR = "/mnt/c/Users/tonio/Projects/myapp/tmp_voices"
os.makedirs(OUTPUT_DIR, exist_ok=True)

VOICES = {
    "ava": "uYXf8XasLslADfZ2MB4u",
    "eli": "c6kFzbpMaJ8UMD5P6l72",
    "finn": "s3TPKV1kjDlVtZbl4Ksh",
    "nora": "6aDn1KB0hjpdcocrUkmq",
    "sarah": "DODLEQrClDo8wCz460ld",
}

results = []

for agent, voice_id in VOICES.items():
    print(f"--- Testing {agent} (voice: {voice_id}) ---")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": f"Hey! I am {agent.capitalize()}, nice to meet you.",
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    resp = requests.post(url, headers=headers, json=payload)
    out_path = os.path.join(OUTPUT_DIR, f"{agent}_hey.mp3")
    with open(out_path, "wb") as f:
        f.write(resp.content)

    size = len(resp.content)
    status = resp.status_code
    print(f"  HTTP: {status} | File: {size} bytes")

    if status == 200:
        print(f"  SUCCESS - saved to {out_path}")
        results.append((agent, "PASS", size))
    else:
        print(f"  FAILED - {resp.text}")
        results.append((agent, "FAIL", resp.text))
    print()

print("=" * 50)
print("SUMMARY")
print("=" * 50)
for agent, result, detail in results:
    if result == "PASS":
        print(f"  {agent:>6}: PASS ({detail} bytes)")
    else:
        print(f"  {agent:>6}: FAIL ({detail})")
