import asyncio, httpx, os, sys
async def main():
    base = os.environ['ASPIRE_SUPABASE_URL'].rstrip('/')
    key = os.environ['ASPIRE_SUPABASE_SERVICE_ROLE_KEY']
    url = f"{base}/storage/v1/object/blueprint-raw-pdfs/94b89098-c4bf-4419-a154-e18d9d53f993/d9ac757e-1a79-4fda-9758-d96b6e8c45f1/raw.pdf"
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.get(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
        sys.stderr.write(f"status={r.status_code} bytes={len(r.content)}\n")
        if r.status_code == 200:
            open("gavnn_source.pdf", "wb").write(r.content)
            sys.stderr.write("saved\n")
asyncio.run(main())
