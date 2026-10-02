"""§6.9 acceptance check: int8 vs fp32 multilingual-e5-small.

    python -m scripts.embedding_eval            # runs both variants in separate processes
    python -m scripts.embedding_eval int8       # one variant, prints a JSON line

Reports recall@5 for vector-only KNN and the full hybrid retriever, plus peak RSS of the
process (VmHWM). Each variant runs in its own process so memory numbers don't mix.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

K = 5


def _peak_rss_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM"):
            return int(line.split()[1]) / 1024
    return float("nan")


async def _run(precision: str, cache_dir: Path) -> dict[str, object]:
    from app.brain import index, retrieval
    from app.brain.embedder import FastEmbedder
    from app.brain.retrieval import Retriever
    from app.brain.store import NoteStore
    from app.db.database import Database
    from app.db.migrate import apply_migrations
    from scripts.eval_data import NOTES, QUERIES

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "eval.db")
        await db.open()
        await db.run_raw(apply_migrations)
        emb = FastEmbedder(precision, cache_dir)  # type: ignore[arg-type]
        t0 = time.perf_counter()
        await emb.warm_up()
        load_s = time.perf_counter() - t0
        root = Path(tmp) / "vault"
        for rel, body in NOTES.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            owner = (
                rel.split("/")[1]
                if rel.startswith("memories/")
                else (rel.split("/")[1][:-3] if rel.startswith("people/") else "shared")
            )
            p.write_text(f"---\nowner: {owner}\ntype: fact\n---\n{body}\n")
        store = NoteStore(root=root, db=db, embedder=emb, tz=ZoneInfo("Asia/Singapore"))
        t0 = time.perf_counter()
        await store.reconcile()
        index_s = time.perf_counter() - t0
        owners = ["jack", "partner", "shared"]
        retriever = Retriever(db, emb)

        stats = {m: {"hit5": 0, "hit1": 0, "rr": 0.0} for m in ("vector", "hybrid")}
        misses: list[dict[str, object]] = []
        for q, expected in QUERIES:
            qv = index.pack(await emb.embed_query(q))
            vec_ids = await db.read(lambda c, qv=qv: retrieval._vec_ids(c, qv))
            info = await db.read(lambda c, ids=vec_ids: retrieval._chunk_info(c, ids))
            ranked = {
                "vector": list(dict.fromkeys(info[i].path for i in vec_ids if i in info)),
                "hybrid": list(
                    dict.fromkeys(
                        h.path for h in await retriever.search(q, owners, k=20) if h.via == "match"
                    )
                ),
            }
            for m, paths in ranked.items():
                rank = paths.index(expected) + 1 if expected in paths else None
                stats[m]["hit5"] += bool(rank and rank <= K)
                stats[m]["hit1"] += rank == 1
                stats[m]["rr"] += 1 / rank if rank else 0.0
                if rank != 1:
                    top = paths[0] if paths else None
                    misses.append({"q": q, "mode": m, "rank": rank, "top": top})
        n = len(QUERIES)
        emb.close()
        await db.close()
    return {
        "precision": precision,
        **{
            f"{m}": f"r@1 {v['hit1']}/{n}  r@5 {v['hit5']}/{n}  mrr {v['rr'] / n:.3f}"
            for m, v in stats.items()
        },
        "load_s": round(load_s, 1),
        "index_s": round(index_s, 1),
        "peak_rss_mb": round(_peak_rss_mb()),
        "misses": misses,
    }


def main() -> None:
    cache = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/fastembed-eval")
    if len(sys.argv) > 1 and sys.argv[1] in ("int8", "fp32"):
        print(json.dumps(asyncio.run(_run(sys.argv[1], cache)), ensure_ascii=False))
        return
    for precision in ("int8", "fp32"):
        out = subprocess.run(
            [sys.executable, "-m", "scripts.embedding_eval", precision, str(cache)],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        print(out.stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    main()
