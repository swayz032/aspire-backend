#!/usr/bin/env python3
import json, os, sys
import requests

def main():
    if len(sys.argv) < 2:
        print("usage: post_robot_run.py <robot_run.json>")
        return 2
    path = sys.argv[1]
    url = os.getenv("ROBOT_INGEST_URL")
    token = os.getenv("ROBOT_INGEST_TOKEN")
    if not url or not token:
        print("ROBOT_INGEST_URL/ROBOT_INGEST_TOKEN must be set")
        return 2
    with open(path, 'r', encoding='utf-8') as f:
        payload = json.load(f)
    r = requests.post(url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    print(r.status_code)
    print(r.text)
    return 0 if r.ok else 1

if __name__ == '__main__':
    raise SystemExit(main())
