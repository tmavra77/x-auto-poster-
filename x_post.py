"""
Post scheduled images to X (Twitter).
Runs via GitHub Actions on a cron schedule.
Posts any row from schedule.csv whose Greece time has arrived.
"""

import csv
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import tweepy

REPO_DIR = Path(__file__).resolve().parent
IMAGES_DIR = REPO_DIR / "images"
SCHEDULE_CSV = REPO_DIR / "schedule.csv"
HISTORY_CSV = REPO_DIR / "history.csv"

HISTORY_FIELDNAMES = ["image", "caption", "scheduled_at", "status", "tweet_id", "posted_at"]

MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB
GREECE_TZ = timezone(timedelta(hours=3))  # EEST (UTC+3)
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def load_config():
    keys = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
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
        print("ERROR: schedule.csv not found")
        sys.exit(1)

    rows = []
    with open(SCHEDULE_CSV, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def write_schedule(rows):
    fieldnames = ["image", "caption", "scheduled_at", "status", "tweet_id"]
    with open(SCHEDULE_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


def validate_image(image_name):
    image_path = IMAGES_DIR / image_name
    if not image_path.exists():
        return None, f"File not found: {image_path}"

    ext = image_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return None, f"Unsupported format: {ext}"

    size = image_path.stat().st_size
    if size > MAX_IMAGE_SIZE:
        return None, f"File too large: {size / 1024 / 1024:.1f} MB (max 5 MB)"

    return image_path, None


def parse_scheduled_time(time_str):
    """Parse time as Greece time (EEST, UTC+3) and convert to UTC."""
    time_str = time_str.strip()
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        dt_greece = dt.replace(tzinfo=GREECE_TZ)
        dt_utc = dt_greece.astimezone(timezone.utc)
        return dt_utc, None
    except ValueError:
        return None, f"Invalid date format: '{time_str}' (expected YYYY-MM-DD HH:MM)"


def process_posts():
    config = load_config()
    rows = read_schedule()
    now = datetime.now(timezone.utc)
    changed = False

    pending = [(i, row) for i, row in enumerate(rows) if not row.get("status")]
    if not pending:
        print("No pending posts.")
        return False

    auth = tweepy.OAuth1UserHandler(
        config["X_API_KEY"],
        config["X_API_SECRET"],
        config["X_ACCESS_TOKEN"],
        config["X_ACCESS_TOKEN_SECRET"],
    )
    api_v1 = tweepy.API(auth)
    client_v2 = tweepy.Client(
        consumer_key=config["X_API_KEY"],
        consumer_secret=config["X_API_SECRET"],
        access_token=config["X_ACCESS_TOKEN"],
        access_token_secret=config["X_ACCESS_TOKEN_SECRET"],
    )

    for idx, row in pending:
        image_name = row.get("image", "").strip()
        caption = row.get("caption", "").strip()
        scheduled_at_str = row.get("scheduled_at", "").strip()

        print(f"--- {image_name} ---")

        # Parse and check time
        scheduled_dt, err = parse_scheduled_time(scheduled_at_str)
        if err:
            print(f"  SKIP: {err}")
            rows[idx]["status"] = "failed"
            rows[idx]["tweet_id"] = err
            changed = True
            continue

        if scheduled_dt > now:
            greece_str = scheduled_dt.astimezone(GREECE_TZ).strftime("%Y-%m-%d %H:%M")
            print(f"  Not yet due (scheduled for {greece_str} Greece time)")
            continue

        # Validate image
        image_path, err = validate_image(image_name)
        if err:
            print(f"  SKIP: {err}")
            rows[idx]["status"] = "failed"
            rows[idx]["tweet_id"] = err
            changed = True
            continue

        if len(caption) > 280:
            print(f"  SKIP: Caption too long ({len(caption)} chars)")
            rows[idx]["status"] = "failed"
            rows[idx]["tweet_id"] = "caption exceeds 280 chars"
            changed = True
            continue

        print(f"  Posting: {caption[:80]}")

        try:
            media = api_v1.media_upload(filename=str(image_path))
            media_id = media.media_id_string
            print(f"  Uploaded media: {media_id}")

            response = client_v2.create_tweet(
                text=caption if caption else None,
                media_ids=[media_id],
            )

            tweet_id = response.data["id"]
            rows[idx]["status"] = "posted"
            rows[idx]["tweet_id"] = tweet_id
            changed = True
            print(f"  Posted! Tweet ID: {tweet_id}")

        except tweepy.TooManyRequests as e:
            print(f"  RATE LIMITED: {e}")
            break

        except tweepy.TweepyException as e:
            error_msg = str(e)
            rows[idx]["status"] = "failed"
            rows[idx]["tweet_id"] = error_msg[:200]
            changed = True
            print(f"  FAILED: {error_msg}")

    if changed:
        now_greece = datetime.now(GREECE_TZ).strftime("%Y-%m-%d %H:%M")
        posted_rows = []
        kept_rows = []

        for row in rows:
            status = row.get("status", "")
            if status == "posted":
                # Move to history and delete image
                posted_rows.append(row)
                image_path = IMAGES_DIR / row.get("image", "").strip()
                if image_path.exists():
                    image_path.unlink()
                    print(f"  Cleaned up image: {image_path.name}")
            else:
                # Keep pending and failed rows (failed stays for visibility + retry)
                kept_rows.append(row)

        # Append posted rows to history.csv
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
            print(f"  Added {len(posted_rows)} row(s) to history.csv")

        write_schedule(kept_rows)
        print(f"\nSchedule updated. {len(posted_rows)} posted, "
              f"{sum(1 for r in kept_rows if r.get('status') == 'failed')} failed (kept for retry).")

    return changed


if __name__ == "__main__":
    changed = process_posts()
    # Set output for GitHub Actions to know if CSV was updated
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
