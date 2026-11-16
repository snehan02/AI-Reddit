"""Validation rules for Reddit tasks and workbook fields."""

import ipaddress
from typing import Optional, Tuple
from urllib.parse import urlsplit

# Non-actionable statuses that indicate a post/comment should not be processed
NON_ACTIONABLE_STATUS_KEYWORDS = [
    "locked post",
    "post deleted",
    "deleted by user",
    "archived post",
    "completed",
]

APPROVED_DOMAINS = frozenset({"reddit.com", "example.com"})
APPROVED_SUBDOMAIN_SUFFIXES = (".reddit.com", ".example.com")
PROHIBITED_METADATA_HOSTS = frozenset({"metadata.google.internal", "instance-data"})


def validate_navigation_url(url: Optional[str]) -> Tuple[bool, str]:
    """Strictly validate destination URL for navigation security.
    
    Allow only explicitly approved domains:
      - reddit.com
      - *.reddit.com
      - example.com (and *.example.com for testing)
      
    Reject:
      - localhost and .localhost
      - loopback addresses (127.0.0.0/8, ::1)
      - private IP addresses (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, etc.)
      - link-local addresses (169.254.0.0/16, fe80::/10)
      - cloud metadata addresses (169.254.169.254, fd00:ec2::254, metadata.google.internal, instance-data)
      - unsupported schemes (only http and https permitted)
      - malformed URLs
      - deceptive domain names (reddit.com.attacker.com, user:pass@attacker.com, etc.)
      - non-standard ports (ports other than 80, 443, or unspecified)
    """
    if url is None:
        return False, "Skipped - invalid URL: URL is empty"
    if not isinstance(url, str) or not url.strip():
        return False, "Skipped - invalid URL: URL is empty"

    cleaned_url = url.strip()

    # Reject whitespace or control characters inside URL
    if any(c in cleaned_url for c in ("\r", "\n", "\t", " ")):
        return False, f"Skipped - invalid URL: URL contains invalid whitespace characters: '{cleaned_url}'"

    try:
        parsed = urlsplit(cleaned_url)
    except Exception as exc:
        return False, f"Skipped - invalid URL: Parse error: {exc}"

    # 1. Scheme check
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"Skipped - invalid URL: Unsupported scheme '{scheme}'. Only http:// and https:// are permitted."

    # 2. Netloc check
    if not parsed.netloc:
        return False, f"Skipped - invalid URL: Missing host component in '{cleaned_url}'"

    # 3. Reject embedded credentials / userinfo
    if parsed.username is not None or parsed.password is not None:
        return False, "Skipped - invalid URL: Deceptive URL with embedded credentials is not permitted."

    # 4. Hostname check
    raw_hostname = parsed.hostname
    if not raw_hostname:
        return False, f"Skipped - invalid URL: Invalid or missing hostname in '{cleaned_url}'"

    # 5. Port check
    if parsed.port is not None and parsed.port not in (80, 443):
        return False, f"Skipped - invalid URL: Disallowed port '{parsed.port}'. Only standard HTTP(S) ports (80, 443) are permitted."

    # 6. Normalize hostname
    normalized_host = raw_hostname.strip().rstrip(".").lower()

    # IDN / Punycode resolution
    try:
        ascii_host = normalized_host.encode("idna").decode("ascii")
    except Exception:
        return False, f"Skipped - invalid URL: Hostname '{normalized_host}' contains unresolvable characters."

    # 7. Check localhost
    if ascii_host == "localhost" or ascii_host.endswith(".localhost"):
        return False, "Skipped - invalid URL: Localhost destinations are strictly prohibited."

    # 8. Check IP addresses (IPv4 & IPv6)
    ip_candidate = ascii_host.strip("[]")
    try:
        ip = ipaddress.ip_address(ip_candidate)
        is_ip = True
    except ValueError:
        is_ip = False

    if is_ip:
        if ip.is_loopback:
            return False, f"Skipped - invalid URL: Loopback address '{ip}' is strictly prohibited."
        if ip.is_link_local:
            return False, f"Skipped - invalid URL: Link-local / metadata address '{ip}' is strictly prohibited."
        if ip.is_private:
            return False, f"Skipped - invalid URL: Private IP address '{ip}' is strictly prohibited."
        if str(ip) == "169.254.169.254" or ip.is_reserved or ip.is_multicast:
            return False, f"Skipped - invalid URL: Cloud metadata or reserved address '{ip}' is strictly prohibited."
        return False, f"Skipped - invalid URL: Direct IP address access '{ip}' is not permitted."

    # Check for decimal/integer or hex representation of IP addresses
    parts = ascii_host.split(".")
    if (ascii_host.isdigit() and len(ascii_host) >= 7) or (
        len(parts) <= 4 and all(p.isdigit() or p.lower().startswith("0x") for p in parts)
    ):
        try:
            if ascii_host.isdigit():
                as_ip = ipaddress.ip_address(int(ascii_host))
                return False, f"Skipped - invalid URL: Encoded IP address '{ascii_host}' ({as_ip}) is strictly prohibited."
        except Exception:
            pass
        return False, f"Skipped - invalid URL: Numeric/encoded IP format '{ascii_host}' is strictly prohibited."

    # 9. Cloud metadata hosts
    if ascii_host in PROHIBITED_METADATA_HOSTS or ascii_host.endswith(".metadata.google.internal"):
        return False, f"Skipped - invalid URL: Cloud metadata host '{ascii_host}' is strictly prohibited."

    # 10. Approved domain check
    is_approved = (
        ascii_host in APPROVED_DOMAINS
        or any(ascii_host.endswith(suffix) for suffix in APPROVED_SUBDOMAIN_SUFFIXES)
    )

    if not is_approved:
        return False, f"Skipped - invalid URL: Unapproved domain '{ascii_host}'. Only 'reddit.com', '*.reddit.com', and 'example.com' are permitted."

    return True, ""


class TaskValidator:
    """Validates fields, URLs, assignments, and statuses against business rules."""

    @staticmethod
    def validate_url(url: Optional[str]) -> Tuple[bool, str]:
        """Validate destination URL against strict navigation security policy."""
        return validate_navigation_url(url)

    @staticmethod
    def validate_account(account_id: Optional[str]) -> Tuple[bool, str]:
        """Validate account ID. Blank account must be skipped."""
        if account_id is None or not str(account_id).strip() or str(account_id).strip().lower() == "none":
            return False, "Skipped - blank account"
        return True, ""

    @staticmethod
    def validate_reply_text(reply: Optional[str]) -> Tuple[bool, str]:
        """Validate comment reply text."""
        if reply is None or not str(reply).strip():
            return False, "Skipped - missing content: Shoro AI reply is empty"
        return True, ""

    @staticmethod
    def validate_post_fields(
        subreddit: Optional[str], title: Optional[str], body: Optional[str]
    ) -> Tuple[bool, str]:
        """Validate post fields: Subreddit, Title, and Body."""
        if not subreddit or not str(subreddit).strip():
            return False, "Skipped - missing content: Subreddit is empty"
        if not title or not str(title).strip():
            return False, "Skipped - missing content: Post title is empty"
        if not body or not str(body).strip():
            return False, "Skipped - missing content: Post body is empty"
        return True, ""

    @staticmethod
    def check_existing_status(status: Optional[str]) -> Tuple[bool, str]:
        """Check if existing status precludes processing.
        
        Returns:
            (is_actionable, reason)
        """
        if status is None:
            return True, ""
        status_clean = str(status).strip()
        if not status_clean:
            return True, ""
            
        status_lower = status_clean.lower()
        for kw in NON_ACTIONABLE_STATUS_KEYWORDS:
            if kw in status_lower:
                return False, f"Skipped - non-actionable status: '{status_clean}'"
                
        # Status like "To review", "Ready for review", etc. are actionable
        return True, ""

