"""Task data models and builders for comments and posts."""

from dataclasses import dataclass
import hashlib
import logging
import re
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def compute_comment_fingerprint(
    worksheet: str,
    excel_row: int,
    destination_url: str,
    account_id: str,
    reply_text: str,
) -> str:
    """Generate stable SHA-256 fingerprint for comment task according to PRD FR-07."""
    payload = f"{worksheet}|{excel_row}|{destination_url.strip()}|{account_id.strip()}|{reply_text.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_post_fingerprint(
    worksheet: str,
    excel_row: int,
    subreddit: str,
    account_id: str,
    title: str,
    body: str,
) -> str:
    """Generate stable SHA-256 fingerprint for post task according to PRD FR-07."""
    payload = f"{worksheet}|{excel_row}|{subreddit.strip()}|{account_id.strip()}|{title.strip()}|{body.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CommentTask:
    task_type: str = "comment"
    worksheet: str = "Driving DMV"
    excel_row: int = 0
    account_id: Optional[str] = None
    chrome_profile_name: Optional[str] = None
    thread_title: str = ""
    destination_url: str = ""
    reply_text: str = ""  # Final prepared text from 'Shoro AI reply' (Column K)
    suggested_reply: Optional[str] = None  # Original draft from 'Suggested reply' (Column G)
    revised_suggested_reply: Optional[str] = None  # Revised draft from 'Revised suggested reply' (Column J)
    why_it_matters: Optional[str] = None  # 'Why it matters / What changed' (Column F)
    status: str = "To review"
    timestamp: Optional[str] = None
    is_eligible: bool = False
    skip_reason: Optional[str] = None
    fingerprint: Optional[str] = None
    task_id: str = ""

    def __post_init__(self):
        if not self.task_id and self.worksheet and self.excel_row:
            self.task_id = f"comment_{self.worksheet.replace(' ', '_')}_{self.excel_row}"
        if self.account_id and self.destination_url and self.reply_text:
            self.fingerprint = compute_comment_fingerprint(
                self.worksheet,
                self.excel_row,
                self.destination_url,
                self.account_id,
                self.reply_text,
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "worksheet": self.worksheet,
            "excel_row": self.excel_row,
            "account_id": self.account_id,
            "chrome_profile_name": self.chrome_profile_name,
            "thread_title": self.thread_title,
            "destination_url": self.destination_url,
            "reply_text": self.reply_text,
            "suggested_reply": self.suggested_reply,
            "revised_suggested_reply": self.revised_suggested_reply,
            "why_it_matters": self.why_it_matters,
            "status": self.status,
            "timestamp": self.timestamp,
            "is_eligible": self.is_eligible,
            "skip_reason": self.skip_reason,
            "fingerprint": self.fingerprint,
        }


@dataclass
class PostTask:
    task_type: str = "post"
    worksheet: str = "Posts"
    excel_row: int = 0
    account_id: Optional[str] = None
    chrome_profile_name: Optional[str] = None
    subreddit: str = ""
    title: str = ""
    body: str = ""
    shoro_link: Optional[str] = None
    status: str = "To review"
    timestamp: Optional[str] = None
    is_eligible: bool = False
    skip_reason: Optional[str] = None
    fingerprint: Optional[str] = None
    task_id: str = ""

    @property
    def destination_url(self) -> str:
        sub = (self.subreddit or "").strip()
        if not sub:
            return "https://www.reddit.com/submit"
        if sub.startswith("http://") or sub.startswith("https://"):
            return sub
        clean_sub = re.sub(r"^/?(r/)+", "", sub, flags=re.IGNORECASE).strip("/")
        return f"https://www.reddit.com/r/{clean_sub}/submit"

    @property
    def assigned_account(self) -> Optional[str]:
        return self.account_id

    def __post_init__(self):
        if not self.task_id and self.worksheet and self.excel_row:
            self.task_id = f"post_{self.worksheet.replace(' ', '_')}_{self.excel_row}"
        if self.account_id and self.subreddit and self.title and self.body:
            self.fingerprint = compute_post_fingerprint(
                self.worksheet,
                self.excel_row,
                self.subreddit,
                self.account_id,
                self.title,
                self.body,
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "worksheet": self.worksheet,
            "excel_row": self.excel_row,
            "account_id": self.account_id,
            "chrome_profile_name": self.chrome_profile_name,
            "subreddit": self.subreddit,
            "title": self.title,
            "body": self.body,
            "shoro_link": self.shoro_link,
            "status": self.status,
            "timestamp": self.timestamp,
            "is_eligible": self.is_eligible,
            "skip_reason": self.skip_reason,
            "fingerprint": self.fingerprint,
        }

