#!/usr/bin/env python3
"""Analyze one explicitly selected, in-scope video using the shared workflow."""
import argparse
import asyncio
import json
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db import engine, async_session, init_db
from src.fetcher import _bili_client, _process_video
from src.lifecycle import HARD_SINCE, owner_context, now, validate_publication
from src.ownership import OwnerGuard
from src.workflow import create_batch, admit, freeze_batch, publication
from src.models import Summary, Video

async def run(args):
    match = re.search(r"BV[a-zA-Z0-9]{10}", args.video)
    if not match:
        raise ValueError("Provide a BVID or its full Bilibili video URL")
    bvid = match.group(0)
    await init_db()
    guard = OwnerGuard(engine, async_session)
    try:
        # The normal service owns this lock. A standalone CLI must never create
        # a second scheduler/writer; use the bounded API retry while it runs.
        await guard.start()
        token = owner_context.set(guard.ticket())
        try:
            async with _bili_client() as client:
                response = await client._get("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid}, signed=False)
                data = response.get("data") or {}
                payload = {"bvid": bvid, "title": data.get("title", ""), "created": data.get("pubdate"),
                    "length": data.get("duration", 0), "aid": data.get("aid", 0), "play": data.get("stat", {}).get("view", 0)}
                validate_publication(publication(payload), since=HARD_SINCE, until=now())
                mid = int(data.get("owner", {}).get("mid") or 0)
                if not args.execute:
                    print(json.dumps({"bvid": bvid, "mid": mid, "publish_time": publication(payload).isoformat(),
                        "queued": False, "message": "Use --execute for this explicit single-video batch"}, ensure_ascii=False))
                    return
                batch = await create_batch(async_session, origin="manual_backfill", since=HARD_SINCE,
                    until=now(), mid=mid, max_videos=1, max_pages=1)
                accepted = await admit(async_session, batch, mid, payload, allow_legacy=True)
                await freeze_batch(async_session, batch)
                if accepted:
                    await _process_video(client, mid, payload, True)
                async with async_session() as session:
                    summary = await session.get(Summary, bvid)
                    result = {"bvid": bvid, "summary": summary.summary if summary else None, "batch_id": batch}
                output = json.dumps(result, ensure_ascii=False, indent=2)
                if args.output:
                    Path(args.output).write_text(output, encoding="utf-8")
                else:
                    print(output)
        finally:
            owner_context.reset(token)
    finally:
        await guard.close()
        await engine.dispose()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--execute", action="store_true", help="Explicitly authorize this one selected video")
    parser.add_argument("--output")
    asyncio.run(run(parser.parse_args()))
