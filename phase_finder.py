#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASM 1995 三元实验相图检索工具

从 IMAGES/*/xxx.TIF 左上角 OCR 识别体系名（如 Ag-Al-As），建立索引并支持检索。

用法:
  python phase_finder.py index          # 建立/续建索引
  python phase_finder.py search Ag-Al-As
  python phase_finder.py search Ag Al As
  python phase_finder.py gui           # 图形界面
  python phase_finder.py stats
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Paths (script / frozen exe)
# ---------------------------------------------------------------------------

def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        # PyInstaller: data next to the executable
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _app_root()
IMAGES_DIR = ROOT / "IMAGES"


def _resolve_db() -> Path:
    candidates = [
        ROOT / "phase_index.db",
        ROOT / "_internal" / "phase_index.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    return ROOT / "phase_index.db"


DB_PATH = _resolve_db()
DEFAULT_WORKERS = max(1, min(6, (os.cpu_count() or 4) - 1))

# ---------------------------------------------------------------------------
# Element dictionary & OCR correction
# ---------------------------------------------------------------------------

ELEMENTS = {
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne",
    "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar",
    "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Ga", "Ge", "As", "Se", "Br", "Kr",
    "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    "In", "Sn", "Sb", "Te", "I", "Xe",
    "Cs", "Ba", "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy",
    "Ho", "Er", "Tm", "Yb", "Lu",
    "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi",
    "Po", "At", "Rn",
    "Fr", "Ra", "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf",
}

# Common OCR misreads for serif scanned labels -> preferred element
OCR_ALIASES = {
    "Ci": "Cu", "Clu": "Cu", "Ch": "Cu", "Cu": "Cu",
    "T1": "Ti", "Tl": "Tl", "T|": "Ti", "T!": "Ti", "Ti": "Ti",
    "R": "B",  # only applied when allow_b_from_r=True
    "An": "Ag", "A1": "Al", "Aa": "As", "As": "As",
    "Mh": "Mn", "Nn": "Mn",
    "0s": "Os", "0": "O",
    "l": "I", "1": "I",
    "VV": "W", "Vv": "W",
    "2n": "Zn",
    "Ng": "Mg", "Mq": "Mg",
    "Nr": "Ni", "Nl": "Ni",
    "Fc": "Fe",
    "Ya": "Ta",
    "Sn": "Sn", "Sm": "Sm",
    "Te": "Te", "Tc": "Tc",
    "Pb": "Pb", "Ph": "Pb",
    "Sb": "Sb", "Sh": "Sb", "5b": "Sb",
    "Bi": "Bi", "Ri": "Bi", "Rl": "Bi", "Bl": "Bi",
    "Ag": "Ag", "Ao": "Ag", "Aq": "Ag", "A9": "Ag",
}


def normalize_element_token(token: str) -> str:
    token = re.sub(r"[^A-Za-z]", "", token or "")
    if not token:
        return ""
    return token[0].upper() + token[1:].lower()


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins, delete, sub = cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def correct_element(token: str, allow_b_from_r: bool = False, fuzzy: bool = False) -> Optional[str]:
    """Map an OCR token to a valid element symbol, or None."""
    raw = (token or "").strip()
    if not raw:
        return None

    def _apply_alias(key: str) -> Optional[str]:
        if key not in OCR_ALIASES:
            return None
        alias = OCR_ALIASES[key]
        if alias == "B" and not allow_b_from_r:
            return None
        return alias if alias in ELEMENTS else None

    # Keep digits for aliases like T1 / Cu1 before stripping
    for key in (raw, normalize_element_token(raw), re.sub(r"\d+", "", raw)):
        if not key:
            continue
        if key in ELEMENTS:
            return key
        aliased = _apply_alias(key)
        if aliased:
            return aliased

    # 1/0 lookalikes
    lookalike = raw.replace("1", "l").replace("0", "O").replace("|", "l")
    lookalike = normalize_element_token(lookalike)
    if lookalike in ELEMENTS:
        return lookalike
    aliased = _apply_alias(lookalike)
    if aliased:
        return aliased

    if not fuzzy:
        return None

    t = normalize_element_token(raw) or lookalike
    if not t or len(t) < 2:
        # Never fuzzy-match single letters (A->P etc.)
        return None
    best = None
    best_d = 99
    for el in ELEMENTS:
        d = _levenshtein(t.lower(), el.lower())
        if d < best_d:
            best_d = d
            best = el
    if best is not None and best_d <= 1 and abs(len(t) - len(best)) <= 1:
        return best
    return None

def format_system(elements: Sequence[str]) -> str:
    return "-".join(elements)


def system_key(elements: Sequence[str]) -> str:
    """Order-independent key for matching."""
    return "|".join(sorted(normalize_element_token(e) for e in elements if e))


def parse_query(text: str) -> List[str]:
    """Parse user query into element list. Accepts Ag-Al-As / Ag Al As / AgAlAs."""
    text = (text or "").strip()
    if not text:
        return []
    text = text.replace("—", "-").replace("–", "-").replace(",", " ").replace(";", " ")
    parts = re.split(r"[\s\-/\\|+]+", text)
    parts = [p for p in parts if p]
    if len(parts) == 1 and re.fullmatch(r"[A-Za-z]{2,6}", parts[0]):
        # greedy parse concatenated symbols, longest-first
        s = parts[0]
        els: List[str] = []
        i = 0
        symbols = sorted(ELEMENTS, key=len, reverse=True)
        while i < len(s) and len(els) < 3:
            matched = False
            for el in symbols:
                if s[i : i + len(el)].lower() == el.lower():
                    els.append(el)
                    i += len(el)
                    matched = True
                    break
            if not matched:
                i += 1
        return els
    out: List[str] = []
    for p in parts:
        el = correct_element(p) or normalize_element_token(p)
        if el:
            out.append(el if el in ELEMENTS else normalize_element_token(p))
    return out[:3]


# ---------------------------------------------------------------------------
# OCR label extraction
# ---------------------------------------------------------------------------

_OCR = None  # per-process RapidOCR instance


def _worker_init() -> None:
    # Avoid oversubscription when many OCR processes run
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("ORT_NUM_THREADS", "1")
    _get_ocr()  # warm up model once per worker


def _get_ocr():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    return _OCR


def _preprocess_label(path: Path, scale: int = 4, box: str = "default"):
    from PIL import Image, ImageOps

    im = Image.open(path).convert("L")
    w, h = im.size
    # Several top-left boxes: some labels sit slightly lower / wider
    boxes = {
        "default": (5, 2, min(int(w * 0.38), 820), min(max(int(h * 0.07), 100), 130)),
        "wide": (5, 0, min(int(w * 0.45), 900), min(max(int(h * 0.09), 110), 150)),
        "tight": (5, 2, min(int(w * 0.32), 700), min(max(int(h * 0.055), 80), 110)),
    }
    left, top, right, bottom = boxes.get(box, boxes["default"])
    crop = im.crop((left, top, right, bottom))
    crop = crop.point(lambda x: 255 if x > 180 else 0)
    crop = ImageOps.expand(crop, border=24, fill=255)
    crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.NEAREST)
    return crop


_SYSTEM_PAT = re.compile(
    r"^([A-Za-z]{1,2})\s*[-–—]\s*([A-Za-z0-9]{1,3})\s*[-–—]\s*([A-Za-z0-9]{1,3})$"
)


def _parse_ocr_texts(texts: Sequence[str], fuzzy: bool = False) -> Tuple[Optional[str], str]:
    joined = " | ".join(texts)
    lower = joined.lower()
    if "reaction" in lower and "scheme" in lower:
        return None, joined

    candidates: List[str] = []
    for t in texts:
        t2 = t.strip().replace(" ", "").replace("—", "-").replace("–", "-")
        t2 = re.sub(r"[^A-Za-z0-9\-]", "", t2)
        m = _SYSTEM_PAT.match(t2)
        if m:
            candidates.append("-".join(m.groups()))
    if not candidates and texts:
        raw = re.sub(
            r"[^A-Za-z0-9\-]",
            "",
            "".join(texts).replace(" ", "").replace("—", "-").replace("–", "-").replace("_", "-"),
        )
        parts = [p for p in raw.split("-") if p]
        if len(parts) >= 3:
            candidates.append("-".join(parts[:3]))

    for cand in candidates:
        parts = cand.split("-")
        allow_r = parts and normalize_element_token(re.sub(r"\d+", "", parts[0])) == "R"
        els: List[str] = []
        ok = True
        for i, p in enumerate(parts[:3]):
            el = correct_element(p, allow_b_from_r=(allow_r and i == 0), fuzzy=fuzzy)
            if el is None:
                p2 = re.sub(r"\d+", "", p)
                el = correct_element(p2, allow_b_from_r=(allow_r and i == 0), fuzzy=fuzzy)
            if el is None:
                ok = False
                break
            els.append(el)
        if ok and len(els) == 3:
            return format_system(els), joined
    return None, joined


def _ocr_texts(ocr, arr, use_det: bool = False) -> Tuple[List[str], float]:
    """OCR crop; return (texts, mean_confidence)."""
    if use_det:
        result, _ = ocr(arr, use_det=True, use_cls=False, use_rec=True)
    else:
        result, _ = ocr(arr, use_det=False, use_cls=False, use_rec=True)
    texts: List[str] = []
    scores: List[float] = []
    if not result:
        return texts, 0.0
    for item in result:
        # det on: [box, text, score] ; det off: [text, score]
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            if isinstance(item[0], str):
                texts.append(item[0])
                scores.append(float(item[1]))
            elif len(item) >= 3 and isinstance(item[1], str):
                texts.append(item[1])
                scores.append(float(item[2]))
    conf = sum(scores) / len(scores) if scores else 0.0
    return texts, conf


def _has_ambiguous_short_element(system_name: str) -> bool:
    """True if the last element looks like a truncated 2-letter symbol."""
    parts = system_name.split("-")
    if not parts:
        return False
    last = parts[-1]
    if len(last) != 1:
        return False
    return any(el.startswith(last) and len(el) == 2 for el in ELEMENTS)


def extract_system_from_image(path: Path) -> Tuple[Optional[str], str, float]:
    """
    Returns (system_name or None, raw_ocr_joined, confidence_proxy).
    Fast path: recognition-only. Fallback: detection + recognition.
    """
    import numpy as np

    ocr = _get_ocr()
    best: Tuple[Optional[str], str, float] = (None, "", 0.0)
    fast_hit: Optional[Tuple[str, str, float]] = None

    fast_attempts = (
        (4, "default"),
        (4, "wide"),
        (3, "default"),
    )
    for scale, box in fast_attempts:
        crop = _preprocess_label(path, scale=scale, box=box)
        texts, conf = _ocr_texts(ocr, np.array(crop.convert("RGB")), use_det=False)
        name, joined = _parse_ocr_texts(texts, fuzzy=False)
        if name:
            # Rec-only scores are often ~0.7–0.9; trust unambiguous 2-letter triples
            parts = name.split("-")
            unambiguous = all(len(p) >= 2 for p in parts) and not _has_ambiguous_short_element(
                name
            )
            if unambiguous and conf >= 0.55:
                return name, joined, conf
            if conf >= 0.90 and not _has_ambiguous_short_element(name):
                return name, joined, conf
            if fast_hit is None or conf > fast_hit[2]:
                fast_hit = (name, joined, conf)
        elif conf >= best[2]:
            best = (None, joined, conf)

    # Slow accurate fallback (also used to confirm ambiguous short-element hits)
    for scale, box in ((4, "default"), (4, "wide")):
        crop = _preprocess_label(path, scale=scale, box=box)
        texts, conf = _ocr_texts(ocr, np.array(crop.convert("RGB")), use_det=True)
        name, joined = _parse_ocr_texts(texts, fuzzy=True)
        if name:
            return name, joined, conf
        if conf >= best[2]:
            best = (None, joined, conf)

    if fast_hit is not None:
        return fast_hit
    return best


@dataclass
class IndexRow:
    rel_path: str
    system_name: Optional[str]
    system_key: Optional[str]
    raw_ocr: str
    confidence: float
    status: str  # ok / fail / skip


def _index_one(rel_path: str) -> IndexRow:
    path = IMAGES_DIR / rel_path
    try:
        name, raw, conf = extract_system_from_image(path)
        if name:
            els = name.split("-")
            return IndexRow(rel_path, name, system_key(els), raw, conf, "ok")
        return IndexRow(rel_path, None, None, raw, conf, "fail")
    except Exception as exc:  # noqa: BLE001
        return IndexRow(rel_path, None, None, str(exc), 0.0, "fail")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS diagrams (
    rel_path    TEXT PRIMARY KEY,
    system_name TEXT,
    system_key  TEXT,
    raw_ocr     TEXT,
    confidence  REAL,
    status      TEXT NOT NULL,
    indexed_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_system_name ON diagrams(system_name);
CREATE INDEX IF NOT EXISTS idx_system_key ON diagrams(system_key);
CREATE INDEX IF NOT EXISTS idx_status ON diagrams(status);
"""


def connect_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def list_tif_files() -> List[str]:
    files: List[str] = []
    if not IMAGES_DIR.is_dir():
        return files
    folders = [f for f in IMAGES_DIR.iterdir() if f.is_dir() and f.name.isdigit()]
    for folder in sorted(folders, key=lambda p: int(p.name)):
        for p in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
            if p.suffix.upper() == ".TIF":
                files.append(f"{folder.name}/{p.name}")
    return files


def upsert_rows(conn: sqlite3.Connection, rows: Iterable[IndexRow]) -> None:
    now = time.time()
    conn.executemany(
        """
        INSERT INTO diagrams(rel_path, system_name, system_key, raw_ocr, confidence, status, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rel_path) DO UPDATE SET
            system_name=excluded.system_name,
            system_key=excluded.system_key,
            raw_ocr=excluded.raw_ocr,
            confidence=excluded.confidence,
            status=excluded.status,
            indexed_at=excluded.indexed_at
        """,
        [
            (r.rel_path, r.system_name, r.system_key, r.raw_ocr, r.confidence, r.status, now)
            for r in rows
        ],
    )
    conn.commit()


def build_index(
    workers: int = DEFAULT_WORKERS,
    limit: Optional[int] = None,
    force: bool = False,
    progress_cb=None,
) -> dict:
    files = list_tif_files()
    if limit is not None:
        files = files[:limit]

    conn = connect_db()
    if not force:
        done = {row["rel_path"] for row in conn.execute("SELECT rel_path FROM diagrams")}
        files = [f for f in files if f not in done]

    total = len(files)
    stats = {"total": total, "ok": 0, "fail": 0, "elapsed": 0.0}
    if total == 0:
        conn.close()
        return stats

    t0 = time.time()
    batch: List[IndexRow] = []
    done_n = 0

    with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init) as pool:
        futures = {pool.submit(_index_one, f): f for f in files}
        for fut in as_completed(futures):
            row = fut.result()
            batch.append(row)
            if row.status == "ok":
                stats["ok"] += 1
            else:
                stats["fail"] += 1
            done_n += 1
            if len(batch) >= 32:
                upsert_rows(conn, batch)
                batch.clear()
            if progress_cb:
                progress_cb(done_n, total, row)
            elif done_n % 20 == 0 or done_n == total:
                elapsed = time.time() - t0
                rate = done_n / elapsed if elapsed else 0
                eta = (total - done_n) / rate if rate else 0
                print(
                    f"\r[{done_n}/{total}] ok={stats['ok']} fail={stats['fail']} "
                    f"{rate:.1f}/s ETA {eta/60:.1f}min  last={row.rel_path} {row.system_name or row.status}",
                    end="",
                    flush=True,
                )

    if batch:
        upsert_rows(conn, batch)
    conn.close()
    stats["elapsed"] = time.time() - t0
    print()
    return stats


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@dataclass
class Hit:
    rel_path: str
    system_name: str
    confidence: float
    raw_ocr: str

    @property
    def abs_path(self) -> Path:
        return IMAGES_DIR / self.rel_path


def search_systems(query: str, limit: int = 200) -> List[Hit]:
    els = parse_query(query)
    conn = connect_db()
    hits: List[Hit] = []

    if not els:
        # free-text like on system_name / raw_ocr
        q = f"%{query.strip()}%"
        rows = conn.execute(
            """
            SELECT rel_path, system_name, confidence, raw_ocr FROM diagrams
            WHERE system_name LIKE ? OR raw_ocr LIKE ?
            ORDER BY system_name, rel_path LIMIT ?
            """,
            (q, q, limit),
        ).fetchall()
    elif len(els) == 3:
        key = system_key(els)
        rows = conn.execute(
            """
            SELECT rel_path, system_name, confidence, raw_ocr FROM diagrams
            WHERE system_key = ?
            ORDER BY system_name, rel_path LIMIT ?
            """,
            (key, limit),
        ).fetchall()
        if not rows:
            # fallback: all three appear in name (any OCR order issues)
            name_pat = "%" + "%".join(els) + "%"
            rows = conn.execute(
                """
                SELECT rel_path, system_name, confidence, raw_ocr FROM diagrams
                WHERE system_name LIKE ? OR (
                    system_name LIKE ? AND system_name LIKE ? AND system_name LIKE ?
                )
                ORDER BY system_name, rel_path LIMIT ?
                """,
                (name_pat, f"%{els[0]}%", f"%{els[1]}%", f"%{els[2]}%", limit),
            ).fetchall()
    else:
        # 1 or 2 elements: contain all
        sql = "SELECT rel_path, system_name, confidence, raw_ocr FROM diagrams WHERE status='ok'"
        params: List[object] = []
        for el in els:
            sql += " AND (system_name LIKE ? OR system_key LIKE ?)"
            params.extend([f"%{el}%", f"%{el}%"])
        sql += " ORDER BY system_name, rel_path LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()

    conn.close()
    for r in rows:
        hits.append(
            Hit(
                rel_path=r["rel_path"],
                system_name=r["system_name"] or "",
                confidence=r["confidence"] or 0.0,
                raw_ocr=r["raw_ocr"] or "",
            )
        )
    return hits


def print_stats() -> None:
    conn = connect_db()
    total = conn.execute("SELECT COUNT(*) c FROM diagrams").fetchone()["c"]
    ok = conn.execute("SELECT COUNT(*) c FROM diagrams WHERE status='ok'").fetchone()["c"]
    fail = conn.execute("SELECT COUNT(*) c FROM diagrams WHERE status='fail'").fetchone()["c"]
    uniq = conn.execute(
        "SELECT COUNT(DISTINCT system_key) c FROM diagrams WHERE system_key IS NOT NULL"
    ).fetchone()["c"]
    files = len(list_tif_files())
    conn.close()
    print(f"TIFF 文件总数 : {files}")
    print(f"已索引       : {total}")
    print(f"  成功识别   : {ok}")
    print(f"  失败/非图  : {fail}")
    print(f"不重复体系   : {uniq}")
    print(f"索引库       : {DB_PATH}")


def open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


# Chinese names for common elements (GUI dropdown labels)
ELEMENT_CN = {
    "H": "氢", "He": "氦", "Li": "锂", "Be": "铍", "B": "硼", "C": "碳", "N": "氮",
    "O": "氧", "F": "氟", "Ne": "氖", "Na": "钠", "Mg": "镁", "Al": "铝", "Si": "硅",
    "P": "磷", "S": "硫", "Cl": "氯", "Ar": "氩", "K": "钾", "Ca": "钙", "Sc": "钪",
    "Ti": "钛", "V": "钒", "Cr": "铬", "Mn": "锰", "Fe": "铁", "Co": "钴", "Ni": "镍",
    "Cu": "铜", "Zn": "锌", "Ga": "镓", "Ge": "锗", "As": "砷", "Se": "硒", "Br": "溴",
    "Kr": "氪", "Rb": "铷", "Sr": "锶", "Y": "钇", "Zr": "锆", "Nb": "铌", "Mo": "钼",
    "Tc": "锝", "Ru": "钌", "Rh": "铑", "Pd": "钯", "Ag": "银", "Cd": "镉", "In": "铟",
    "Sn": "锡", "Sb": "锑", "Te": "碲", "I": "碘", "Xe": "氙", "Cs": "铯", "Ba": "钡",
    "La": "镧", "Ce": "铈", "Pr": "镨", "Nd": "钕", "Pm": "钷", "Sm": "钐", "Eu": "铕",
    "Gd": "钆", "Tb": "铽", "Dy": "镝", "Ho": "钬", "Er": "铒", "Tm": "铥", "Yb": "镱",
    "Lu": "镥", "Hf": "铪", "Ta": "钽", "W": "钨", "Re": "铼", "Os": "锇", "Ir": "铱",
    "Pt": "铂", "Au": "金", "Hg": "汞", "Tl": "铊", "Pb": "铅", "Bi": "铋", "Po": "钋",
    "At": "砹", "Rn": "氡", "Fr": "钫", "Ra": "镭", "Ac": "锕", "Th": "钍", "Pa": "镤",
    "U": "铀", "Np": "镎", "Pu": "钚", "Am": "镅", "Cm": "锔", "Bk": "锫", "Cf": "锎",
}


def element_label(symbol: str) -> str:
    cn = ELEMENT_CN.get(symbol, "")
    return f"{symbol}-{cn}" if cn else symbol


def parse_element_label(label: str) -> str:
    """Extract symbol from 'Al-铝' / 'Al' / empty."""
    label = (label or "").strip()
    if not label or label in ("（不限）", "(any)", "-"):
        return ""
    return label.split("-", 1)[0].strip()


def list_indexed_elements() -> List[str]:
    """Unique element symbols appearing in the index (for dropdowns)."""
    if not DB_PATH.exists():
        return sorted(ELEMENTS, key=lambda s: (len(s), s))
    conn = connect_db()
    rows = conn.execute(
        "SELECT DISTINCT system_name FROM diagrams WHERE status='ok' AND system_name IS NOT NULL"
    ).fetchall()
    conn.close()
    found = set()
    for r in rows:
        for part in (r["system_name"] or "").split("-"):
            if part in ELEMENTS:
                found.add(part)
    return sorted(found or ELEMENTS, key=lambda s: (s.lower(), s))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def run_gui() -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    from PIL import Image, ImageTk

    root = tk.Tk()
    root.title("ASM 三元实验相图检索  —  Handbook of Ternary Alloy Phase Diagrams (1995)")
    root.geometry("1180x720")
    root.minsize(960, 600)

    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass

    main = ttk.Frame(root, padding=8)
    main.pack(fill=tk.BOTH, expand=True)
    main.columnconfigure(0, weight=1)
    main.rowconfigure(0, weight=1)

    # ---- Left: diagram viewer ----
    left = ttk.Frame(main)
    left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
    left.rowconfigure(0, weight=1)
    left.columnconfigure(0, weight=1)

    canvas = tk.Canvas(left, bg="#f4f4f4", highlightthickness=1, highlightbackground="#aaa")
    canvas.grid(row=0, column=0, sticky="nsew")
    hbar = ttk.Scrollbar(left, orient=tk.HORIZONTAL, command=canvas.xview)
    vbar = ttk.Scrollbar(left, orient=tk.VERTICAL, command=canvas.yview)
    hbar.grid(row=1, column=0, sticky="ew")
    vbar.grid(row=0, column=1, sticky="ns")
    canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)

    placeholder = canvas.create_text(
        40,
        40,
        anchor="nw",
        fill="#666",
        font=("Microsoft YaHei UI", 12),
        text="请在右侧选择三个组分，点击「显示相图」\n也可在结果列表中切换同一体系的多张等温截面图",
    )

    # ---- Right: controls ----
    right = ttk.Frame(main, width=300)
    right.grid(row=0, column=1, sticky="ns")
    right.grid_propagate(False)

    ttk.Label(
        right,
        text="三元实验相图",
        font=("Microsoft YaHei UI", 14, "bold"),
    ).pack(anchor=tk.W, pady=(4, 2))
    ttk.Label(
        right,
        text="ASM International · 1995",
        foreground="#666",
    ).pack(anchor=tk.W, pady=(0, 12))

    el_symbols = list_indexed_elements()
    el_labels = ["（不限）"] + [element_label(s) for s in el_symbols]

    mat1 = tk.StringVar(value=element_label("Al") if "Al" in el_symbols else el_labels[1])
    mat2 = tk.StringVar(value=element_label("As") if "As" in el_symbols else el_labels[min(2, len(el_labels) - 1)])
    mat3 = tk.StringVar(value=element_label("Ag") if "Ag" in el_symbols else el_labels[min(3, len(el_labels) - 1)])

    def _combo(parent, text, var):
        ttk.Label(parent, text=text).pack(anchor=tk.W, pady=(8, 2))
        cb = ttk.Combobox(parent, textvariable=var, values=el_labels, state="readonly", width=28)
        cb.pack(fill=tk.X)
        return cb

    _combo(right, "材料 1（组分 A）", mat1)
    _combo(right, "材料 2（组分 B）", mat2)
    _combo(right, "材料 3（组分 C）", mat3)

    ttk.Label(right, text="快速输入（可选）").pack(anchor=tk.W, pady=(14, 2))
    qvar = tk.StringVar()
    qentry = ttk.Entry(right, textvariable=qvar)
    qentry.pack(fill=tk.X)
    ttk.Label(
        right,
        text="例: Ag-Al-As 或 Fe Ni（覆盖上方选择）",
        foreground="#666",
        wraplength=250,
    ).pack(anchor=tk.W, pady=(2, 8))

    status = tk.StringVar(value="就绪")
    ttk.Label(right, textvariable=status, wraplength=250, foreground="#333").pack(
        anchor=tk.W, pady=(0, 8)
    )

    hits_cache: List[Hit] = []
    photo_ref = {"img": None, "path": None}  # keep reference to avoid GC

    def _fit_image(path: Path) -> Optional[ImageTk.PhotoImage]:
        try:
            im = Image.open(path)
            # Group4 1-bit TIF → RGB for display
            im = im.convert("RGB")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("无法打开图片", f"{path}\n{exc}")
            return None
        canvas.update_idletasks()
        cw = max(canvas.winfo_width() - 16, 200)
        ch = max(canvas.winfo_height() - 16, 200)
        scale = min(cw / im.width, ch / im.height, 1.0)
        # Prefer fitting into view; allow slight upscale for tiny crops only
        if scale < 1.0:
            nw, nh = max(1, int(im.width * scale)), max(1, int(im.height * scale))
            im = im.resize((nw, nh), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(im)

    def show_hit(hit: Hit) -> None:
        path = hit.abs_path
        if not path.exists():
            messagebox.showwarning("文件缺失", f"找不到文件:\n{path}")
            return
        photo = _fit_image(path)
        if photo is None:
            return
        canvas.delete("all")
        photo_ref["img"] = photo
        photo_ref["path"] = path
        canvas.create_image(8, 8, anchor="nw", image=photo)
        canvas.config(scrollregion=(0, 0, photo.width() + 16, photo.height() + 16))
        status.set(f"{hit.system_name}    {hit.rel_path}")
        root.title(f"ASM 三元相图 — {hit.system_name}  ({hit.rel_path})")

    def fill_results(hits: List[Hit]) -> None:
        nonlocal hits_cache
        hits_cache = hits
        tree.delete(*tree.get_children())
        for h in hits:
            tree.insert("", tk.END, values=(h.system_name, h.rel_path))
        if hits:
            tree.selection_set(tree.get_children()[0])
            show_hit(hits[0])
            status.set(f"找到 {len(hits)} 张相图")
        else:
            canvas.delete("all")
            canvas.create_text(
                40,
                40,
                anchor="nw",
                fill="#666",
                font=("Microsoft YaHei UI", 12),
                text="未找到匹配的相图\n请更换组分或检查索引是否完整",
            )
            photo_ref["img"] = None
            status.set("未找到结果")

    def current_query() -> str:
        q = qvar.get().strip()
        if q:
            return q
        a, b, c = (
            parse_element_label(mat1.get()),
            parse_element_label(mat2.get()),
            parse_element_label(mat3.get()),
        )
        parts = [p for p in (a, b, c) if p]
        return " ".join(parts)

    def do_show(_event=None) -> None:
        if not DB_PATH.exists():
            messagebox.showwarning(
                "提示",
                f"未找到索引库:\n{DB_PATH}\n\n请先运行「建立索引」或确保 phase_index.db 与程序同目录。",
            )
            return
        if not IMAGES_DIR.is_dir():
            messagebox.showwarning(
                "提示",
                f"未找到 IMAGES 目录:\n{IMAGES_DIR}\n\n请将相图数据文件夹放在程序同目录下。",
            )
            return
        q = current_query()
        if not q:
            messagebox.showinfo("提示", "请至少选择一个组分，或在快速输入框中输入体系。")
            return
        hits = search_systems(q, limit=300)
        fill_results(hits)

    def on_select(_event=None) -> None:
        sel = tree.selection()
        if not sel:
            return
        idx = tree.index(sel[0])
        if 0 <= idx < len(hits_cache):
            show_hit(hits_cache[idx])

    def open_external() -> None:
        if photo_ref["path"]:
            open_path(Path(photo_ref["path"]))
        elif hits_cache:
            open_path(hits_cache[0].abs_path)

    def reveal_folder() -> None:
        path = Path(photo_ref["path"]) if photo_ref["path"] else None
        if path is None and hits_cache:
            path = hits_cache[0].abs_path
        if path is None:
            return
        if sys.platform.startswith("win"):
            subprocess.run(["explorer", "/select,", str(path)], check=False)
        else:
            open_path(path.parent)

    def on_resize(_event=None) -> None:
        if not photo_ref["path"]:
            return
        path = Path(photo_ref["path"])
        job = getattr(on_resize, "_job", None)
        if job is not None:
            try:
                root.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass

        def _refit():
            hit = next((h for h in hits_cache if h.abs_path == path), None)
            if hit:
                show_hit(hit)

        on_resize._job = root.after(250, _refit)  # type: ignore[attr-defined]

    btn_show = ttk.Button(right, text="显示相图", command=do_show)
    btn_show.pack(fill=tk.X, pady=(6, 4), ipady=4)

    ttk.Label(right, text="结果列表（同一体系可能有多张图）").pack(anchor=tk.W, pady=(10, 2))
    tree_frm = ttk.Frame(right)
    tree_frm.pack(fill=tk.BOTH, expand=True)
    cols = ("system", "path")
    tree = ttk.Treeview(tree_frm, columns=cols, show="headings", height=12)
    tree.heading("system", text="体系")
    tree.heading("path", text="文件")
    tree.column("system", width=100, anchor=tk.W)
    tree.column("path", width=140, anchor=tk.W)
    tvsb = ttk.Scrollbar(tree_frm, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=tvsb.set)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    tvsb.pack(side=tk.RIGHT, fill=tk.Y)

    ttk.Button(right, text="用系统查看器打开", command=open_external).pack(fill=tk.X, pady=(8, 2))
    ttk.Button(right, text="打开所在文件夹", command=reveal_folder).pack(fill=tk.X, pady=2)
    ttk.Button(right, text="退出", command=root.destroy).pack(fill=tk.X, pady=(16, 4))

    tree.bind("<<TreeviewSelect>>", on_select)
    tree.bind("<Double-1>", lambda e: open_external())
    qentry.bind("<Return>", do_show)
    canvas.bind("<Configure>", on_resize)

    if DB_PATH.exists():
        try:
            conn = connect_db()
            n = conn.execute("SELECT COUNT(*) c FROM diagrams WHERE status='ok'").fetchone()["c"]
            conn.close()
            status.set(f"索引已加载（{n} 张）")
        except Exception:  # noqa: BLE001
            status.set("索引库异常")
    else:
        status.set("缺少 phase_index.db")
        canvas.itemconfigure(
            placeholder,
            text="未找到索引库 phase_index.db\n请将其与程序放在同一目录",
        )

    # Default demo: Ag-Al-As if available
    if DB_PATH.exists() and IMAGES_DIR.is_dir():
        root.after(300, do_show)

    root.mainloop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ASM 1995 三元实验相图检索工具")
    sub = parser.add_subparsers(dest="cmd")

    p_index = sub.add_parser("index", help="OCR 建立/续建索引")
    p_index.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p_index.add_argument("--limit", type=int, default=None, help="仅处理前 N 张（测试用）")
    p_index.add_argument("--force", action="store_true", help="强制重建全部")

    p_search = sub.add_parser("search", help="检索体系")
    p_search.add_argument("query", nargs="+", help="体系，如 Ag-Al-As 或 Ag Al As")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.add_argument("--open", action="store_true", help="打开第一条结果")

    sub.add_parser("gui", help="打开图形界面")
    sub.add_parser("stats", help="显示索引统计")

    args = parser.parse_args(argv)
    if not args.cmd:
        # default to GUI when double-clicked / no args
        run_gui()
        return 0

    if args.cmd == "index":
        print(f"IMAGES: {IMAGES_DIR}")
        print(f"DB:     {DB_PATH}")
        print(f"workers={args.workers} force={args.force} limit={args.limit}")
        st = build_index(workers=args.workers, limit=args.limit, force=args.force)
        print(
            f"完成: 处理 {st['total']} 张, ok={st['ok']}, fail={st['fail']}, "
            f"用时 {st['elapsed']/60:.1f} 分钟"
        )
        print_stats()
        return 0

    if args.cmd == "search":
        if not DB_PATH.exists():
            print("尚未建立索引。请先运行: python phase_finder.py index")
            return 1
        q = " ".join(args.query)
        hits = search_systems(q, limit=args.limit)
        if not hits:
            print(f"未找到: {q}")
            return 2
        print(f"查询「{q}」→ {len(hits)} 条\n")
        for h in hits:
            print(f"  {h.system_name:12}  {h.rel_path}")
        if args.open and hits:
            open_path(hits[0].abs_path)
        return 0

    if args.cmd == "stats":
        print_stats()
        return 0

    if args.cmd == "gui":
        run_gui()
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    # Windows ProcessPool needs freeze_support
    import multiprocessing as mp

    mp.freeze_support()
    raise SystemExit(main())
