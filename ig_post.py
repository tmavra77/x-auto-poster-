"""
Post scheduled images to Instagram via Buffer API.
Runs via GitHub Actions on a cron schedule.
Posts any row from ig_schedule.csv whose Greece time has arrived.
"""

import csv
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

REPO_DIR = Path(__file__).resolve().parent
IG_IMAGES_DIR = REPO_DIR / "ig-images"
SCHEDULE_CSV = REPO_DIR / "ig_schedule.csv"
HISTORY_CSV = REPO_DIR / "ig_history.csv"

FIELDNAMES = ["image", "caption", "scheduled_at", "status", "buffer_post_id"]
HISTORY_FIELDNAMES = ["image", "caption", "scheduled_at", "status", "buffer_post_id", "posted_at"]

GREECE_TZ = timezone(timedelta(hours=3))
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_CAPTION_LENGTH = 2200

GRAPHQL_URL = "https://api.buffer.com"
REPO_RAW_BASE = "https://raw.githubusercontent.com/tmavra77/x-auto-poster-/main/ig-images"


def load_config():
    keys = ["BUFFER_API_KEY", "BUFFER_IG_CHANNEL_ID"]
    config = {}
    for k in keys:
        val = os.environ.get(k, "")
        if not val:
            print(f"ERROR: Missing environment variable: {k}")
            sys.exit(1)
        config[k] = val
    return config


def read_schedule():
    if not SCHEDULE_CSV.exists():
        print("ERROR: ig_schedule.csv not found")
        sys.exit(1)

    rows = []
    with open(SCHEDULE_CSV, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def write_schedule(rows):
    with open(SCHEDULE_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


def validate_image(image_name):
    image_path = IG_IMAGES_DIR / image_name
    if not image_path.exists():
        return None, f"File not found: {image_path}"

    ext = image_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return None, f"Unsupported format: {ext}"

    return image_path, None


def parse_scheduled_time(time_str):
    time_str = time_str.strip()
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        dt_greece = dt.replace(tzinfo=GREECE_TZ)
        dt_utc = dt_greece.astimezone(timezone.utc)
        return dt_utc, None
    except ValueError:
        return None, f"Invalid date format: '{time_str}' (expected YYYY-MM-DD HH:MM)"


def post_to_buffer(caption, image_url, channel_id, api_key):
    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess {
          post {
            id
            status
          }
        }
        ... on MutationError {
          message
        }
      }
    }
    """

    variables = {
        "input": {
            "text": caption,
            "channelId": channel_id,
            "schedulingType": "automatic",
            "mode": "shareNow",
            "assets": {
                "images": [{"url": image_url}]
            },
            "metadata": {
                "instagram": {
                    "type": "post",
                    "shouldShareToFeed": True
                }
            }
        }
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        GRAPHQL_URL,
        json={"query": mutation, "variables": variables},
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()

    result = data.get("data", {}).get("createPost", {})

    post_data = result.get("post")
    if post_data:
        post_id = post_data.get("id", "unknown")
        post_status = post_data.get("status", "unknown")
        print(f"  Buffer post status: {post_status}")
        if post_status in ("failed", "error"):
            return False, f"Buffer accepted post but status={post_status} (id={post_id})"
        return True, post_id

    error_msg = result.get("message", "Unknown error from Buffer")
    return False, error_msg


def process_posts():
    config = load_config()
    rows = read_schedule()
    now = datetime.now(timezone.utc)
    changed = False

    pending = [(i, row) for i, row in enumerate(rows) if not row.get("status")]
    if not pending:
        print("No pending posts.")
        return False

    for idx, row in pending:
        image_name = row.get("image", "").strip()
        caption = row.get("caption", "").strip()
        scheduled_at_str = row.get("scheduled_at", "").strip()

        print(f"--- {image_name} ---")

        scheduled_dt, err = parse_scheduled_time(scheduled_at_str)
        if err:
            print(f"  SKIP: {err}")
            rows[idx]["status"] = "failed"
            rows[idx]["buffer_post_id"] = err
            changed = True
            continue

        if scheduled_dt > now:
            greece_str = scheduled_dt.astimezone(GREECE_TZ).strftime("%Y-%m-%d %H:%M")
            print(f"  Not yet due (scheduled for {greece_str} Greece time)")
            continue

        image_path, err = validate_image(image_name)
        if err:
            print(f"  SKIP: {err}")
            rows[idx]["status"] = "failed"
            rows[idx]["buffer_post_id"] = err
            changed = True
            continue

        if len(caption) > MAX_CAPTION_LENGTH:
            print(f"  SKIP: Caption too long ({len(caption)} chars)")
            rows[idx]["status"] = "failed"
            rows[idx]["buffer_post_id"] = f"caption exceeds {MAX_CAPTION_LENGTH} chars"
            changed = True
            continue

        image_url = f"{REPO_RAW_BASE}/{requests.utils.quote(image_name)}"
        print(f"  Posting: {caption[:80]}")
        print(f"  Image URL: {image_url}")

        try:
            ok, result = post_to_buffer(
                caption, image_url,
                config["BUFFER_IG_CHANNEL_ID"],
                config["BUFFER_API_KEY"],
            )

            if ok:
                rows[idx]["status"] = "posted"
                rows[idx]["buffer_post_id"] = result
                changed = True
                print(f"  Posted! Buffer post ID: {result}")
            else:
                rows[idx]["status"] = "failed"
                rows[idx]["buffer_post_id"] = result[:200]
                changed = True
                print(f"  FAILED: {result}")

        except requests.exceptions.Timeout:
            print("  TIMEOUT: Buffer API did not respond in time")
            rows[idx]["status"] = "failed"
            rows[idx]["buffer_post_id"] = "timeout"
            changed = True

        except Exception as e:
            error_msg = str(e)
            rows[idx]["status"] = "failed"
            rows[idx]["buffer_post_id"] = error_msg[:200]
            changed = True
            print(f"  FAILED: {error_msg}")

    if changed:
        now_greece = datetime.now(GREECE_TZ).strftime("%Y-%m-%d %H:%M")
        posted_rows = []
        kept_rows = []

        for row in rows:
            status = row.get("status", "")
            if status == "posted":
                posted_rows.append(row)
                image_path = IG_IMAGES_DIR / row.get("image", "").strip()
                if image_path.exists():
                    image_path.unlink()
                    print(f"  Cleaned up image: {image_path.name}")
            else:
                kept_rows.append(row)

        if posted_rows:
            history_exists = HISTORY_CSV.exists()
            with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f, fieldnames=HISTORY_FIELDNAMES,
                    quoting=csv.QUOTE_ALL, extrasaction="ignore")
                if not history_exists:
                    writer.writeheader()
                for row in posted_rows:
                    row["posted_at"] = now_greece
                    writer.writerow(row)
            print(f"  Added {len(posted_rows)} row(s) to ig_history.csv")

        write_schedule(kept_rows)
        print(f"\nSchedule updated. {len(posted_rows)} posted, "
              f"{sum(1 for r in kept_rows if r.get('status') == 'failed')} failed (kept for retry).")

    return changed


if __name__ == "__main__":
    changed = process_posts()
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
