# ============================================================
# 9. 정적 사이트 내보내기  (노트북 셀 1~6 을 먼저 실행한 뒤 이 셀을 실행)
#
#   결과물: /content/mood_reader_site/
#     ├─ index.html            ← /content 에 index.html 을 올려 두면 자동 복사
#     ├─ data/books.json       ← 카탈로그 + 무드 정의 + 곡 목록(라이선스 포함)
#     ├─ data/books/{sn}.json  ← 작품별 페이지 본문 + 8개 무드 점수 + 상위 감정
#     └─ audio/{sn}.mp3        ← 무드별 배경음악
#   마지막에 zip 으로 묶어 내려받는다. → GitHub Pages 등에 그대로 올리면 끝.
#
#   곡 배정은 사이트(브라우저)에서 무드 점수로 다시 계산한다.
#   그래서 사이트에서 '전환 민감도'를 바꾸면 곡이 바뀌는 페이지도 즉시 달라진다.
# ============================================================
import os, json, shutil, random
from concurrent.futures import ThreadPoolExecutor

EXPORT = {
    "N_BOOKS": 60,             # 사이트에 넣을 작품 수 (많을수록 분석 시간 증가)
    "MIN_PAGES": 3,            # 너무 짧은 작품 제외
    "MAX_PAGES": 60,           # 너무 긴 작품 제외 (분석 시간·용량 절약)
    "TRACKS_PER_MOOD": 6,      # 무드별로 사이트에 넣을 곡 수
    "MAX_AUDIO_BYTES": 4_000_000,
    "OUT_DIR": "/content/mood_reader_site",
    "SEED": 7,
}
OUT = EXPORT["OUT_DIR"]
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(f"{OUT}/data/books", exist_ok=True)
os.makedirs(f"{OUT}/audio", exist_ok=True)
_EXT = {"audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/wav": "wav", "audio/ogg": "ogg"}


# ---------- 1) 곡 수집 (라이선스 표기를 위해 검색 결과를 다시 받아 license 를 보존) ----------
def _collect_candidates(pages=2):
    tasks = [(g, kw, p) for g in MOOD_GROUPS for kw in MOOD_GROUPS[g]["gongu_kw"] for p in range(1, pages + 1)]

    def fetch(kw, p):
        try:
            return gongu.search_page(kw, "music", "popular", p)[0]
        except Exception:
            return []

    with ThreadPoolExecutor(CONFIG["WORKERS"]) as ex:
        futs = {(g, kw, p): ex.submit(fetch, kw, p) for g, kw, p in tasks}
    cands, seen = {g: [] for g in MOOD_GROUPS}, set()
    for g, kw, p in tasks:                                   # 그룹·키워드 순서 유지, 곡은 한 그룹에만
        for it in futs[(g, kw, p)].result():
            blob = it["title"] + " " + " ".join(it["tags"])
            if it["wrtSn"] in seen or any(b in blob for b in _MUSIC_BLOCK):
                continue
            seen.add(it["wrtSn"]); cands[g].append(it)
    return cands


def _download_track(it):
    try:
        data, mime = gongu.music_bytes(it["wrtSn"], EXPORT["MAX_AUDIO_BYTES"])
        fname = f"audio/{it['wrtSn']}.{_EXT.get(mime, 'mp3')}"
        with open(f"{OUT}/{fname}", "wb") as f:
            f.write(data)
        return fname, len(data)
    except Exception:
        return None, 0


print("① 무드별 곡 수집 중 …")
_cands = _collect_candidates()
TRACKS = {}
with ThreadPoolExecutor(CONFIG["WORKERS"]) as ex:
    for g, items in _cands.items():
        want = EXPORT["TRACKS_PER_MOOD"]
        trial = items[: want + 4]                            # 실패 대비 여유분
        for it, (fname, size) in zip(trial, ex.map(_download_track, trial)):
            if fname and sum(1 for t in TRACKS.values() if t["g"] == g) < want:
                TRACKS[it["wrtSn"]] = {"g": g, "title": it["title"], "artist": it["author"],
                                       "url": it["url"], "license": it["license"], "file": fname}
            elif fname:
                os.remove(f"{OUT}/{fname}")                  # 초과분은 삭제
        print(f"   {MOOD_GROUPS[g]['emoji']} {MOOD_GROUPS[g]['name']}: "
              f"{sum(1 for t in TRACKS.values() if t['g'] == g)}곡")


# ---------- 2) 작품 선정 · 페이지 분할 · 감정 분석 ----------
def _load(b):
    try:
        return b, prepare_book(gongu.book_text(b["wrtSn"]), b["title"], b["author"])
    except Exception:
        return b, None


def _default_seq(scores_list):
    """노트북 _read_ahead 와 같은 규칙(전환 직후 문턱 2배)으로 페이지별 무드 순서를 만든다."""
    seq, prev, last = [], None, -10
    for i, s in enumerate(scores_list):
        margin = CONFIG["SWITCH_MARGIN"] * (2 if i - last <= 1 else 1)
        g = choose_group(s, prev, margin)
        if g != prev:
            last = i
        seq.append(g); prev = g
    return seq


print("② 작품 분석 중 … (CPU 기준 작품당 수 초~수십 초)")
BOOKS, _batch = [], 16
for start in range(0, len(_catalog), _batch):
    if len(BOOKS) >= EXPORT["N_BOOKS"]:
        break
    with ThreadPoolExecutor(CONFIG["WORKERS"]) as ex:
        loaded = list(ex.map(_load, _catalog[start: start + _batch]))
    for b, pages in loaded:
        if len(BOOKS) >= EXPORT["N_BOOKS"]:
            break
        if not pages or not (EXPORT["MIN_PAGES"] <= len(pages) <= EXPORT["MAX_PAGES"]):
            continue
        out_pages, scores_list = [], []
        for text in pages:
            a = engine.analyze(text)
            s = {g: round(float(v), 3) for g, v in a["groups"].items()}
            scores_list.append(s)
            out_pages.append({"text": text, "s": s,
                              "top": [[l, round(float(v), 3)] for l, v in a["top"][:3]]})
        book = {"sn": b["wrtSn"], "title": b["title"], "author": b["author"], "tags": b["tags"][:6],
                "url": b["url"], "license": b.get("license", ""), "pages": out_pages}
        with open(f"{OUT}/data/books/{b['wrtSn']}.json", "w", encoding="utf-8") as f:
            json.dump(book, f, ensure_ascii=False, separators=(",", ":"))
        BOOKS.append({"sn": b["wrtSn"], "title": b["title"], "author": b["author"], "tags": b["tags"][:4],
                      "url": b["url"], "license": b.get("license", ""), "pages": len(pages),
                      "seq": _default_seq(scores_list)})
        print(f"   [{len(BOOKS):>3}/{EXPORT['N_BOOKS']}] {b['title']} — {b['author']} ({len(pages)}p)")


# ---------- 3) 카탈로그 · index.html · zip ----------
catalog = {
    "version": 1,
    "engine": engine.mode,
    "model": CONFIG["KOTE_MODEL"],
    "switch_margin": CONFIG["SWITCH_MARGIN"],
    "moods": {g: {"name": v["name"], "emoji": v["emoji"], "hue": v["hue"]} for g, v in MOOD_GROUPS.items()},
    "tracks": TRACKS,
    "books": BOOKS,
}
with open(f"{OUT}/data/books.json", "w", encoding="utf-8") as f:
    json.dump(catalog, f, ensure_ascii=False, separators=(",", ":"))

if os.path.exists("/content/index.html"):
    shutil.copy("/content/index.html", f"{OUT}/index.html")
    print("③ index.html 복사 완료")
else:
    print("③ ⚠ /content/index.html 이 없습니다. 압축을 푼 폴더에 index.html 을 직접 넣어 주세요.")

_size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(OUT) for f in fs)
print(f"   작품 {len(BOOKS)}편 · 곡 {len(TRACKS)}곡 · 전체 {_size / 1e6:.1f} MB · 엔진 {engine.mode}")
if engine.mode == "lexicon":
    print("   ⚠ KOTE 모델이 아니라 간이 사전으로 분석됐습니다. 모델 로드 오류를 먼저 확인하세요.")

_zip = shutil.make_archive("/content/mood_reader_site", "zip", OUT)
if in_colab():
    from google.colab import files
    files.download(_zip)
print("완료:", _zip)
