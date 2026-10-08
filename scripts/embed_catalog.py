# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#     "numpy>=1.26",
#     "scipy>=1.13",
#     "soundfile>=0.13",
#     "sentence-transformers>=6.1",
#     "transformers>=5.19",
#     "torch==2.11.0",
#     "torchaudio==2.11.0",
#     "torchvision==0.26.0",
# ]
#
# [tool.uv.sources]
# torch = { index = "pytorch-cu128" }
# torchaudio = { index = "pytorch-cu128" }
# torchvision = { index = "pytorch-cu128" }
#
# [[tool.uv.index]]
# name = "pytorch-cu128"
# url = "https://download.pytorch.org/whl/cu128"
# explicit = true
# ///
import argparse
import gc
import json
import os
import re
import sqlite3
import sys
from collections import deque
from collections.abc import Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

MODEL = "google/embeddinggemma-2"
DIM = 768
SR = 48000
ENCODER_SR = 16000
CLIP = 10 * SR
CLIP_STARTS = (0.2, 0.5, 0.8)
WINDOW = 96
TRACKS_PER_BATCH = 8
CHUNK = 500
IMPORT_CHUNK = 2000
VRAM_BYTES = 5 * 2**30
BUSY_S = 120
LENGTH_TOLERANCE = 0.1
SILENT_RMS = 1e-3
LANGS = (None, "ru", "foreign")
CYRILLIC = re.compile("[\u0400-\u04ff]")
VECTOR = np.dtype("<f2")
FLOATS = np.dtype("<f4")
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS track_vectors(track_id INTEGER PRIMARY KEY, vec BLOB NOT NULL)",
    "CREATE TABLE IF NOT EXISTS vector_space(key TEXT PRIMARY KEY, value BLOB NOT NULL)",
    "CREATE TABLE IF NOT EXISTS moods(id TEXT PRIMARY KEY, title TEXT NOT NULL, "
    "grp TEXT NOT NULL, ord INTEGER NOT NULL, prompt TEXT NOT NULL, lang TEXT, "
    "vec BLOB NOT NULL)",
)
OK_TRACKS = (
    "SELECT t.id, t.path, t.dur, t.artist, t.title, a.genre FROM tracks t "
    "LEFT JOIN albums a ON a.id = t.album_id WHERE t.status = 'ok' ORDER BY t.id"
)


@dataclass(frozen=True)
class Track:
    id: int
    path: str
    dur: int | None
    russian: bool


@dataclass(frozen=True)
class Decoded:
    track_id: int
    clips: np.ndarray | None
    error: str | None
    flag: str | None


@dataclass(frozen=True)
class Mood:
    id: str
    title: str
    group: str
    prompt: str
    lang: str | None


def open_read(catalog: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{catalog.resolve().as_uri()}?mode=ro", uri=True, timeout=BUSY_S)


def open_write(catalog: Path) -> sqlite3.Connection:
    db = sqlite3.connect(catalog, timeout=BUSY_S)
    with db:
        for statement in SCHEMA:
            db.execute(statement)
    return db


def has_table(db: sqlite3.Connection, name: str) -> bool:
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
    return found.fetchone() is not None


def is_russian(artist: str | None, title: str | None, genre: str | None) -> bool:
    return bool(
        CYRILLIC.search(artist or "")
        or CYRILLIC.search(title or "")
        or (genre or "").startswith("RU")
    )


def ok_tracks(catalog: Path) -> list[Track]:
    with closing(open_read(catalog)) as db:
        rows = db.execute(OK_TRACKS).fetchall()
    return [Track(r[0], r[1], r[2], is_russian(r[3], r[4], r[5])) for r in rows]


def vector_ids(catalog: Path) -> set[int]:
    with closing(open_read(catalog)) as db:
        if not has_table(db, "track_vectors"):
            return set()
        return {row[0] for row in db.execute("SELECT track_id FROM track_vectors")}


def space_value(db: sqlite3.Connection, key: str) -> bytes | str | None:
    row = db.execute("SELECT value FROM vector_space WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def claim_space(db: sqlite3.Connection) -> None:
    stored = space_value(db, "model")
    if stored is not None and stored != MODEL:
        raise SystemExit(f"vector_space holds {stored!r} vectors, this tool makes {MODEL!r}")
    with db:
        db.execute("INSERT OR IGNORE INTO vector_space VALUES ('model', ?)", (MODEL,))
        db.execute("INSERT OR IGNORE INTO vector_space VALUES ('dim', ?)", (str(DIM),))


def write_vectors(catalog: Path, rows: Sequence[tuple[int, bytes]]) -> None:
    with closing(open_write(catalog)) as db:
        claim_space(db)
        with db:
            db.executemany("INSERT OR REPLACE INTO track_vectors VALUES (?, ?)", rows)


def import_vectors(catalog: Path, directory: Path) -> int:
    vectors = np.load(directory / "emb_gemma.npy")
    ids = json.loads((directory / "ids.json").read_text(encoding="utf-8"))
    if vectors.dtype != np.float16 or vectors.shape != (len(ids), DIM):
        raise SystemExit(f"{directory}: expected float16 [{len(ids)}, {DIM}], got {vectors.shape}")
    with closing(open_read(catalog)) as db:
        known = {row[0] for row in db.execute("SELECT id FROM tracks")}
    rows = [
        (i, v.astype(VECTOR).tobytes()) for i, v in zip(ids, vectors, strict=True) if i in known
    ]
    for start in range(0, len(rows), IMPORT_CHUNK):
        write_vectors(catalog, rows[start : start + IMPORT_CHUNK])
    print(f"imported {len(rows)} of {len(ids)} vectors", flush=True)
    return len(rows)


def resolve(path: str, library: Path) -> Path:
    stored = Path(path)
    return stored if stored.is_absolute() else library / stored


def cut_clips(path: Path) -> tuple[np.ndarray, float]:
    info = sf.info(path)
    total, native = info.frames, info.samplerate
    length = 10 * native
    clips = np.zeros((len(CLIP_STARTS), CLIP), np.float16)
    for k, fraction in enumerate(CLIP_STARTS):
        start = min(max(int(fraction * total) - length // 2, 0), max(total - length, 0))
        audio, _ = sf.read(path, start=start, frames=length, dtype="float32", always_2d=True)
        mono = audio.mean(1)
        if native != SR:
            from scipy.signal import resample_poly

            divisor = np.gcd(SR, native)
            mono = resample_poly(mono, SR // divisor, native // divisor).astype(np.float32)[:CLIP]
        clips[k, : len(mono)] = mono
    return clips, total / native


def flag_of(clips: np.ndarray, seconds: float, dur: int | None) -> str | None:
    if not dur or abs(seconds - dur) > LENGTH_TOLERANCE * dur:
        return "length"
    rms = np.sqrt((clips.astype(np.float32) ** 2).mean(1))
    return "silent" if (rms < SILENT_RMS).any() else None


def decode(job: tuple[int, str, int | None]) -> Decoded:
    track_id, path, dur = job
    try:
        clips, seconds = cut_clips(Path(path))
    except Exception as error:
        return Decoded(track_id, None, f"{type(error).__name__}: {str(error)[:200]}", None)
    return Decoded(track_id, clips, None, flag_of(clips, seconds, dur))


def decode_stream(jobs: Sequence[tuple[int, str, int | None]], workers: int) -> Iterator[Decoded]:
    source = iter(jobs)
    pending = deque()
    with ProcessPoolExecutor(workers) as pool:
        while True:
            while len(pending) < WINDOW and (job := next(source, None)) is not None:
                pending.append(pool.submit(decode, job))
            if not pending:
                return
            yield pending.popleft().result()


class AudioEncoder:
    def __init__(self) -> None:
        import torch
        import torchaudio
        from sentence_transformers import SentenceTransformer

        self.torch = torch
        self.resample = torchaudio.functional.resample
        total = torch.cuda.get_device_properties(0).total_memory
        torch.cuda.set_per_process_memory_fraction(min(1.0, VRAM_BYTES / total))
        self.model = SentenceTransformer(
            MODEL,
            device="cuda",
            config_kwargs={"vision_config": None},
            model_kwargs={"torch_dtype": torch.bfloat16, "attn_implementation": "sdpa"},
        )
        self.encode(np.full((len(CLIP_STARTS), CLIP), 0.01, np.float16))

    def encode(self, clips: np.ndarray) -> np.ndarray:
        wave = self.torch.from_numpy(clips).float().to("cuda")
        audio = self.resample(wave, SR, ENCODER_SR).cpu().numpy()
        embedded = self.model.encode(
            [{"audio": a} for a in audio],
            batch_size=len(audio),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        ).astype(np.float32)
        mean = embedded.reshape(-1, len(CLIP_STARTS), embedded.shape[-1]).mean(1)
        return mean / np.linalg.norm(mean, axis=1, keepdims=True)

    def close(self) -> None:
        del self.model
        gc.collect()
        self.torch.cuda.empty_cache()


def encode_batches(
    encoder: AudioEncoder, decoded: Iterator[Decoded]
) -> Iterator[tuple[int, bytes]]:
    batch: list[Decoded] = []
    for item in decoded:
        batch.append(item)
        if len(batch) == TRACKS_PER_BATCH:
            yield from encode_batch(encoder, batch)
            batch = []
    if batch:
        yield from encode_batch(encoder, batch)


def encode_batch(encoder: AudioEncoder, batch: Sequence[Decoded]) -> Iterator[tuple[int, bytes]]:
    clips = [item.clips for item in batch if item.clips is not None]
    vectors = encoder.encode(np.concatenate(clips))
    for item, vector in zip(batch, vectors, strict=True):
        yield item.track_id, vector.astype(VECTOR).tobytes()


def report_decoded(decoded: Iterator[Decoded], counts: dict[str, int]) -> Iterator[Decoded]:
    for item in decoded:
        if item.error:
            counts["failed"] += 1
            print(f"track {item.track_id}: {item.error}", flush=True)
            continue
        if item.flag:
            counts[item.flag] += 1
        yield item


def embed_missing(catalog: Path, library: Path, missing: Sequence[Track], workers: int) -> int:
    if not missing:
        return 0
    jobs = [(t.id, str(resolve(t.path, library)), t.dur) for t in missing]
    counts = {"failed": 0, "length": 0, "silent": 0}
    print(f"embedding {len(jobs)} tracks", flush=True)
    encoder = AudioEncoder()
    stored, chunk = 0, []
    try:
        decoded = report_decoded(decode_stream(jobs, workers), counts)
        for row in encode_batches(encoder, decoded):
            chunk.append(row)
            if len(chunk) == CHUNK:
                write_vectors(catalog, chunk)
                stored += len(chunk)
                chunk = []
                print(f"stored {stored} of {len(jobs)}", flush=True)
        if chunk:
            write_vectors(catalog, chunk)
            stored += len(chunk)
    finally:
        encoder.close()
    print(f"embedded {stored} of {len(jobs)}, flagged {counts}", flush=True)
    return stored


def drop_stale(catalog: Path, ok_ids: set[int]) -> int:
    stale = sorted(vector_ids(catalog) - ok_ids)
    if not stale:
        return 0
    with closing(open_write(catalog)) as db, db:
        db.executemany("DELETE FROM track_vectors WHERE track_id = ?", [(i,) for i in stale])
    print(f"dropped {len(stale)} vectors of tracks that are no longer ok", flush=True)
    return len(stale)


def unit(vector: np.ndarray) -> np.ndarray:
    return vector / np.linalg.norm(vector)


def fit_space(vectors: np.ndarray, russian: np.ndarray) -> tuple[bytes, bytes]:
    if not russian.any() or russian.all():
        raise SystemExit("the vocal-language axis needs both Russian and other tracks")
    mean = vectors.mean(axis=0, dtype=np.float64)
    axis = unit(vectors[russian].mean(axis=0) - vectors[~russian].mean(axis=0))
    return mean.astype(FLOATS).tobytes(), axis.astype(FLOATS).tobytes()


def load_vectors(catalog: Path, tracks: Sequence[Track]) -> tuple[np.ndarray, np.ndarray]:
    flags = {t.id: t.russian for t in tracks}
    with closing(open_read(catalog)) as db:
        rows = db.execute("SELECT track_id, vec FROM track_vectors ORDER BY track_id").fetchall()
    vectors = np.frombuffer(b"".join(r[1] for r in rows), VECTOR).reshape(-1, DIM)
    vectors = vectors.astype(np.float32)
    return vectors, np.array([flags[r[0]] for r in rows])


def refit(catalog: Path, tracks: Sequence[Track]) -> bool:
    vectors, russian = load_vectors(catalog, tracks)
    if not len(vectors):
        return False
    mean, axis = fit_space(vectors, russian)
    with closing(open_write(catalog)) as db:
        claim_space(db)
        if space_value(db, "mean") == mean and space_value(db, "lang_axis") == axis:
            print("mean and language axis unchanged", flush=True)
            return False
        version = int(space_value(db, "version") or 0) + 1
        with db:
            db.execute("INSERT OR REPLACE INTO vector_space VALUES ('mean', ?)", (mean,))
            db.execute("INSERT OR REPLACE INTO vector_space VALUES ('lang_axis', ?)", (axis,))
            db.execute("INSERT OR REPLACE INTO vector_space VALUES ('version', ?)", (str(version),))
    print(f"mean and language axis recomputed over {len(vectors)} vectors, version {version}")
    return True


def load_moods(path: Path) -> list[Mood]:
    moods = []
    for entry in json.loads(path.read_text(encoding="utf-8")):
        mood = Mood(entry["id"], entry["title"], entry["group"], entry["prompt"], entry.get("lang"))
        if mood.lang not in LANGS or not (mood.id and mood.title and mood.group and mood.prompt):
            raise SystemExit(f"{path}: bad mood {entry!r}")
        moods.append(mood)
    if len({m.id for m in moods}) != len(moods):
        raise SystemExit(f"{path}: duplicate mood ids")
    return moods


def encode_texts(texts: Sequence[str]) -> np.ndarray:
    import torch
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(
        MODEL,
        device="cpu",
        config_kwargs={"vision_config": None, "audio_config": None},
        model_kwargs={"torch_dtype": torch.float32},
    )
    return model.encode(
        list(texts),
        prompt_name="query",
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )


def stored_moods(catalog: Path) -> dict[str, tuple]:
    with closing(open_read(catalog)) as db:
        if not has_table(db, "moods"):
            return {}
        rows = db.execute("SELECT id, title, grp, ord, prompt, lang FROM moods").fetchall()
    return {r[0]: tuple(r[1:]) for r in rows}


def sync_moods(catalog: Path, path: Path) -> int:
    moods = load_moods(path)
    rows = {m.id: (m.title, m.group, order, m.prompt, m.lang) for order, m in enumerate(moods)}
    stored = stored_moods(catalog)
    stale = sorted(stored.keys() - rows.keys())
    changed = [i for i, row in rows.items() if stored.get(i) != row]
    if not stale and not changed:
        print(f"{len(moods)} moods unchanged", flush=True)
        return 0
    fresh = [i for i in changed if i not in stored or stored[i][3] != rows[i][3]]
    vectors = (
        dict(zip(fresh, encode_texts([rows[i][3] for i in fresh]), strict=True)) if fresh else {}
    )
    with closing(open_write(catalog)) as db, db:
        db.executemany("DELETE FROM moods WHERE id = ?", [(i,) for i in stale])
        for mood_id in changed:
            title, group, order, prompt, lang = rows[mood_id]
            if mood_id in vectors:
                blob = vectors[mood_id].astype(VECTOR).tobytes()
                db.execute(
                    "INSERT OR REPLACE INTO moods VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (mood_id, title, group, order, prompt, lang, blob),
                )
            else:
                db.execute(
                    "UPDATE moods SET title = ?, grp = ?, ord = ?, lang = ? WHERE id = ?",
                    (title, group, order, lang, mood_id),
                )
    print(
        f"moods: {len(fresh)} embedded, {len(changed) - len(fresh)} relabelled, {len(stale)} dropped"
    )
    return len(changed) + len(stale)


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EmbeddingGemma 2 vectors and moods for the catalog"
    )
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--import", dest="import_dir", type=Path, metavar="DIR")
    parser.add_argument("--moods", type=Path, default=Path(__file__).with_name("moods.json"))
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse(argv)
    if args.import_dir:
        import_vectors(args.catalog, args.import_dir)
        return 0
    tracks = ok_tracks(args.catalog)
    ok_ids = {t.id for t in tracks}
    drop_stale(args.catalog, ok_ids)
    have = vector_ids(args.catalog)
    missing = [t for t in tracks if t.id not in have]
    embed_missing(args.catalog, args.library, missing, args.workers)
    refit(args.catalog, tracks)
    sync_moods(args.catalog, args.moods)
    return 0


if __name__ == "__main__":
    sys.exit(main())
