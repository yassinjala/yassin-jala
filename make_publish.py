"""
إرسال المقال إلى Make (Webhook) ليتولى النشر على Blogger.
التشغيل: python make_publish.py  (بعد دفع المخرجات، ومعه OUT_DIR)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from blogger_publish import (
    BloggerError, DEFAULT_LABEL, _flag, _strip_h1, build_post_html,
    fill_first_comment, public_image_url,
)

TIMEOUT = 90          # Make قد يتأخر في الرد
RETRY_STATUS = {429, 500, 502, 503, 504}


def split_labels(requested: str) -> list[str]:
    out: list[str] = []
    for raw in re.split(r"[,،]", requested or ""):
        name = raw.strip()
        if name and name not in out:
            out.append(name)
    return out


def post_to_make(payload: dict[str, Any]) -> dict[str, Any]:
    url = os.getenv("MAKE_WEBHOOK_URL", "").strip()
    if not url:
        raise BloggerError("سر MAKE_WEBHOOK_URL غير موجود.")
    headers = {"Content-Type": "application/json"}
    secret = os.getenv("MAKE_WEBHOOK_SECRET", "").strip()
    if secret:
        headers["x-make-apikey"] = secret

    last = "فشل الاتصال بـ Make."
    for attempt in (1, 2):
        try:
            r = requests.post(url, headers=headers, timeout=TIMEOUT,
                              data=json.dumps(payload, ensure_ascii=False)
                              .encode("utf-8"))
        except requests.RequestException as exc:
            last = f"تعذر الاتصال بـ Make: {exc}"
        else:
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    return {}          # رد نصي مثل "Accepted"
            last = f"خطأ Make HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code not in RETRY_STATUS:
                raise BloggerError(last)
        if attempt == 1:
            time.sleep(5)
    raise BloggerError(last)


def publish(out_dir: Path) -> dict[str, Any]:
    result_path = out_dir / "result.json"
    if not result_path.is_file():
        raise BloggerError(f"لا يوجد result.json في {out_dir}.")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    previous = result.get("blogger") or {}

    if previous.get("post_id") and not _flag("FORCE_REPROCESS"):
        print(f"المقال منشور سابقًا ({previous.get('url')}); لا حاجة للنشر.")
        fill_first_comment(out_dir, result, previous.get("url"))
        return previous

    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    sha = os.getenv("BLOGGER_COMMIT_SHA", "").strip()
    if not repo or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise BloggerError("GITHUB_REPOSITORY / BLOGGER_COMMIT_SHA غير صالحين.")

    md_file = out_dir / "rewritten_article.md"
    if not md_file.is_file():
        raise BloggerError("rewritten_article.md غير موجود.")
    markdown = _strip_h1(md_file.read_text(encoding="utf-8"))

    workspace = Path(os.getenv("GITHUB_WORKSPACE") or Path.cwd()).resolve()

    def rel(name: str) -> str:
        return Path(os.path.relpath((out_dir / name).resolve(),
                                    workspace)).as_posix()

    urls: dict[str, str] = {}
    for name in ("thumbnail.jpg", "article_middle.jpg"):
        if (out_dir / name).is_file():
            urls[name] = public_image_url(repo, sha, rel(name))

    title = result["title"]
    content = build_post_html(
        markdown, urls.get("thumbnail.jpg"),
        result.get("image_title") or title,
        resolve_src=lambda src: urls.get(src, src),
    )

    requested = (os.getenv("BLOGGER_LABEL", "").strip()
                 or str(result.get("category") or "").strip())
    labels = split_labels(requested) or [DEFAULT_LABEL]
    draft = _flag("BLOGGER_DRAFT")

    payload = {
        "title": title,
        "content": content,
        "labels": labels,
        "labels_csv": ", ".join(labels),   # إن أرادها Make نصًا واحدًا
        "draft": draft,
        "status": "DRAFT" if draft else "LIVE",
        "post_id": previous.get("post_id") or "",   # للتحديث عبر Router
        "source_url": result.get("source_url"),
        "thumbnail_url": urls.get("thumbnail.jpg", ""),
        "images": urls,
        "seo": result.get("seo"),
    }

    reply = post_to_make(payload)
    post_url = reply.get("url") or None

    record = {
        "via": "make",
        "post_id": reply.get("post_id") or None,
        "url": post_url,
        "status": reply.get("status") or ("DRAFT" if draft else "SENT"),
        "labels": labels,
        "action": "update" if previous.get("post_id") else "insert",
        "images": urls,
        "commit": sha,
        "sent_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    if post_url:
        fill_first_comment(out_dir, result, post_url)
    else:
        print("::warning title=رابط المقال::لم يرد Make برابط المقال "
              "(أضف Webhook response)؛ بقي {LINK} في first_comment.txt.")

    result["blogger"] = record
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2)
                           + "\n", encoding="utf-8")
    print(f"أُرسل المقال إلى Make: {post_url or '(بلا رابط بعد)'}")
    return record


def main() -> int:
    out = os.getenv("OUT_DIR", "").strip()
    if not out:
        print("::error::OUT_DIR غير محدد.", file=sys.stderr)
        return 1
    try:
        publish(Path(out))
    except BloggerError as exc:
        print(f"::error title=فشل الإرسال إلى Make::{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"::error title=عطل غير متوقع::{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
