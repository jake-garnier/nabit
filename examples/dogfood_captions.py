"""Dogfooding nabit against REAL silent-failure patterns in the captions system.

Modeled on captions/tasks/:
  * scraping_tasks.py:292 extract_caption_task — runs OCR, writes a ScrapedCaption
    row, updates Video.processing_status, then returns "success". If the OCR
    produced an empty caption or the row didn't persist, "success" is a lie.
  * scraping_tasks.py:104 download_video_task — downloads a file, writes a Video
    row. "success" should mean the file is actually on disk at storage_path.
  * caption_judge.py:260 judge_backlog_batch — an LLM judge writes scores; a
    "passed" caption with all-zero scores is a silent failure.

Faithful simulation with an in-memory "DB" and a temp file. The post-conditions
re-query real state (dict/filesystem) exactly like the real checks would hit
PostgreSQL / disk. Run: python examples/dogfood_captions.py
"""

import logging
import os
import tempfile

from nabit import verify, Mode, get_results, summary, run
from nabit.checks import all_of, field_equals, truthy, in_range, file_exists, predicate

logging.basicConfig(level=logging.WARNING, format="%(message)s")

# stand-ins for the Postgres tables
SCRAPED_CAPTIONS = {}   # video_id -> {"caption_text": ...}
VIDEOS = {}             # video_id -> {"processing_status": ..., "storage_path": ...}


# ---------------------------------------------------------------------------
# 1. extract_caption_task — "success" must mean a non-empty caption row exists
#    AND the video was marked caption_extracted.
# ---------------------------------------------------------------------------
caption_persisted = all_of(
    predicate(lambda r, ctx: ctx["video_id"] in SCRAPED_CAPTIONS),
    predicate(lambda r, ctx: bool(SCRAPED_CAPTIONS.get(ctx["video_id"], {}).get("caption_text"))),
    predicate(lambda r, ctx: VIDEOS.get(ctx["video_id"], {}).get("processing_status") == "caption_extracted"),
)


@verify(caption_persisted, mode=Mode.WARN, name="extract_caption_task")
def extract_caption_task(video_id, ocr_text):
    VIDEOS.setdefault(video_id, {})
    if ocr_text:                                   # only persist if OCR gave text
        SCRAPED_CAPTIONS[video_id] = {"caption_text": ocr_text}
        VIDEOS[video_id]["processing_status"] = "caption_extracted"
    return {"status": "success", "video_id": video_id}   # claims success regardless


# ---------------------------------------------------------------------------
# 2. download_video_task — "success" must mean the file is actually on disk.
# ---------------------------------------------------------------------------
@verify(file_exists(lambda r, ctx: r["storage_path"]), mode=Mode.WARN,
        name="download_video_task")
def download_video_task(post_id, storage_path, really_write):
    if really_write:
        with open(storage_path, "w") as fh:
            fh.write("fake video bytes")
    VIDEOS[post_id] = {"storage_path": storage_path, "processing_status": "downloaded"}
    return {"status": "success", "storage_path": storage_path}   # claims success regardless


# ---------------------------------------------------------------------------
# 3. judge_backlog_batch — a "passed" caption can't have out-of-range scores.
# ---------------------------------------------------------------------------
judged_ok = all_of(
    field_equals("judge_status", "passed"),
    in_range(1, 10, key="grammar"),
    in_range(1, 10, key="hotness"),
)


@verify(judged_ok, mode=Mode.WARN, name="judge_caption")
def judge_caption(caption_id, scores):
    return {"judge_status": "passed", **scores}


if __name__ == "__main__":
    tmp = tempfile.mkdtemp()
    with run("captions-dogfood") as rid:
        print("A. OCR succeeds -> caption row written ...")
        extract_caption_task(1, ocr_text="POV: you woke up as a cat")

        print("B. OCR returns empty -> task still says success (THE BUG) ...")
        extract_caption_task(2, ocr_text="")                 # <-- nabit catches

        print("C. download writes the file ...")
        download_video_task("p1", os.path.join(tmp, "v1.mp4"), really_write=True)

        print("D. download 'success' but file never hit disk ...")
        download_video_task("p2", os.path.join(tmp, "v2.mp4"), really_write=False)  # <-- caught

        print("E. judge passes a caption with sane scores ...")
        judge_caption(10, {"grammar": 8, "hotness": 7})

        print("F. judge marks 'passed' but scores are 0 (LLM junk) ...")
        judge_caption(11, {"grammar": 0, "hotness": 0})      # <-- nabit catches

    print("\n--- verification log ---")
    for r in get_results(run_id=rid):
        tag = "PASS" if r.passed else "FAIL <-- silent failure caught"
        print(f"  {r.name:24} {tag}")
    print("\n--- summary ---")
    print(" ", summary(run_id=rid))
