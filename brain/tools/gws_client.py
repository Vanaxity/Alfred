"""
Google Workspace Client (Calendar, Gmail, Drive)
Thread-safe singleton wrapper around google-api-python-client.
Replaces brittle gws CLI with native Python API calls.
"""

import json
import logging
import threading
import base64
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Set
from email.message import EmailMessage

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent.parent.parent
TOKEN_FILE = PROJECT_DIR / "data" / "token.json"
CREDS_FILE = PROJECT_DIR / "data" / "credentials.json"

ALL_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.file",
]

SCOPE_MAP = {
    "calendar": [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
    ],
    "gmail.readonly": ["https://www.googleapis.com/auth/gmail.readonly"],
    "gmail.send": ["https://www.googleapis.com/auth/gmail.send"],
    "gmail": [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ],
    "drive.file": ["https://www.googleapis.com/auth/drive.file"],
}


class AuthRequiredError(Exception):
    """Raised when GWS OAuth needs user intervention."""

    def __init__(self, message: str, auth_url: str = None):
        self.auth_url = auth_url
        super().__init__(message)


def save_token(creds: Credentials):
    """Save credentials to token file."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, "w") as token:
        token.write(creds.to_json())
    logger.info(f"Token saved to {TOKEN_FILE}")


def get_auth_url(scopes: List[str] = None) -> str:
    """Get OAuth authorization URL for headless auth flow.

    BROKEN as of 2026-08: Google now rejects the out-of-band redirect_uri
    ("urn:ietf:wg:oauth:2.0:oob") with "OOB flow has been blocked" -
    confirmed live this session. Re-auth was completed instead via
    InstalledAppFlow.run_local_server(), which needs no registered
    redirect_uri and does the full flow in one blocking call, so it doesn't
    fit this function's two-step "return a URL, accept a code later" shape.
    Whatever calls get_auth_url/exchange_code (brain_api/server.py imports
    both) will hit the same wall. Left as-is rather than redesigned here:
    a real fix needs a redirect URI registered in Cloud Console and a
    decision about what the cockpit's reconnect UX should look like.
    """
    if scopes is None:
        scopes = ALL_SCOPES
    if not CREDS_FILE.exists():
        raise FileNotFoundError(f"Credentials file not found at {CREDS_FILE}.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), scopes)
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    auth_url, _ = flow.authorization_url(prompt="consent")
    return auth_url


def exchange_code(authorization_code: str, scopes: List[str] = None) -> dict:
    """Exchange authorization code for token and save it."""
    if scopes is None:
        scopes = ALL_SCOPES
    if not CREDS_FILE.exists():
        raise FileNotFoundError(f"Credentials file not found at {CREDS_FILE}.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), scopes)
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    flow.fetch_token(code=authorization_code)
    creds = flow.credentials
    save_token(creds)
    return {
        "status": "ok",
        "scopes": list(creds.scopes) if creds.scopes else [],
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }


def _get_granted_scopes_from_file() -> Optional[Set[str]]:
    """Read the scopes ACTUALLY granted, straight from the token file's own
    stored data.

    Credentials.from_authorized_user_file(path, scopes) sets creds.scopes to
    whatever `scopes` list you pass in, not what the token was really issued
    with -- confirmed live: a token granted with only calendar access still
    reported all 5 requested scopes as present via creds.scopes, and the
    gap only surfaced as a runtime 403 on the first Gmail call. Reading the
    raw file bypasses that.
    """
    if not TOKEN_FILE.exists():
        return None
    try:
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        return None
    raw = data.get("scopes", data.get("scope"))
    if raw is None:
        return None
    if isinstance(raw, str):
        return set(raw.split())
    return set(raw)


def get_token_status() -> dict:
    """Check current token status without triggering auth flow."""
    if not TOKEN_FILE.exists():
        return {"authenticated": False, "reason": "no_token_file"}
    try:
        # scopes=None -> creds.scopes comes from the file's own "scopes" key
        # (the real grant), not an echo of whatever we pass in. Passing
        # ALL_SCOPES here previously masked exactly the drift this function
        # exists to detect.
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
        granted = _get_granted_scopes_from_file()
        return {
            "authenticated": creds.valid,
            "expired": creds.expired if creds.expired is not None else False,
            "has_refresh_token": bool(creds.refresh_token),
            "scopes": sorted(granted) if granted is not None else (list(creds.scopes) if creds.scopes else []),
            "missing_scopes": sorted(set(ALL_SCOPES) - granted) if granted is not None else [],
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
        }
    except Exception as e:
        return {"authenticated": False, "reason": str(e)}


def _get_credentials(scopes: List[str]) -> Credentials:
    """Get OAuth credentials, running refresh if needed.
    Verifies token has the requested scopes — triggers re-auth if missing."""
    creds = None

    if TOKEN_FILE.exists():
        try:
            # scopes=None (not the caller's `scopes` arg) -- see comment in
            # get_token_status(). Passing a caller-supplied scope subset here
            # (e.g. SCOPE_MAP["calendar"], just 2 scopes) made creds.scopes
            # echo that narrower set; refreshing THIS credential and saving
            # it then persisted the narrower set back to token.json, silently
            # downgrading a full 5-scope grant to 2 -- confirmed live: this
            # exact drift happened during today's session between two
            # verification passes, dropping gmail/drive access that had
            # already been granted and tested working.
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
        except Exception as e:
            logger.warning(f"Failed to load token: {e}")
            creds = None

    # ALSO check if token has the right scopes (not just valid/expired).
    # Reads the raw file rather than creds.scopes -- see
    # _get_granted_scopes_from_file() for why creds.scopes can't be trusted
    # for this check.
    missing_scopes = False
    if creds:
        granted = _get_granted_scopes_from_file()
        if granted is not None:
            required = set(scopes)
            if not required.issubset(granted):
                missing_scopes = True
                logger.warning(
                    f"Token missing scopes. Has: {granted}, Needs: {required}"
                )

    if not creds or not creds.valid or missing_scopes:
        if creds and creds.refresh_token and not missing_scopes:
            try:
                creds.refresh(Request())
                save_token(creds)
            except RefreshError as e:
                logger.error(f"Token refresh failed: {e}")
                creds = None
            except Exception as e:
                logger.error(f"Unexpected refresh error: {e}")
                creds = None

        if not creds or not creds.valid or missing_scopes:
            # Fresh re-auth with the exact scopes needed. missing_scopes is
            # checked here too -- a currently-valid-but-under-scoped token
            # would otherwise sail past this guard and get returned anyway,
            # only to 403 on the actual API call.
            auth_url = get_auth_url(scopes)
            raise AuthRequiredError(
                "GWS authentication required with scopes: " + ", ".join(scopes),
                auth_url=auth_url,
            )

    return creds


class GWSClient:
    """Thread-safe singleton Google Workspace client."""

    _instance = None
    _lock = threading.Lock()
    _init_lock = threading.Lock()
    _creds_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self._creds = None
            self._calendar_service = None
            self._gmail_service = None
            self._drive_service = None
            self._initialized = True
            logger.info("GWSClient initialized (lazy-init mode)")

    def _ensure_creds(self, scopes: Optional[List[str]] = None):
        if scopes is None:
            scopes = ALL_SCOPES
        with self._creds_lock:
            if self._creds is None or not self._creds.valid:
                self._creds = _get_credentials(scopes)

    def _get_calendar_service(self):
        if self._calendar_service is None:
            self._ensure_creds(SCOPE_MAP["calendar"])
            self._calendar_service = build("calendar", "v3", credentials=self._creds)
        return self._calendar_service

    def _get_gmail_service(self):
        if self._gmail_service is None:
            self._ensure_creds(SCOPE_MAP["gmail"])
            self._gmail_service = build("gmail", "v1", credentials=self._creds)
        return self._gmail_service

    def _get_drive_service(self):
        if self._drive_service is None:
            self._ensure_creds(SCOPE_MAP["drive.file"])
            self._drive_service = build("drive", "v3", credentials=self._creds)
        return self._drive_service

    def _handle_api_error(self, e: Exception, context: str) -> str:
        if isinstance(e, RefreshError):
            return "Google auth expired. Run: python -m brain.tools.gws_client --auth --scopes calendar,gmail,drive.file"
        if isinstance(e, HttpError):
            return f"Google API error ({context}): {e.resp.status} {e.reason}"
        return f"Google API error ({context}): {e}"

    # ============ CALENDAR ============

    def get_agenda(self, days: int = 7, max_results: int = 10) -> str:
        try:
            service = self._get_calendar_service()
            now = datetime.now(timezone.utc).isoformat()
            later = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

            events_result = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=now,
                    timeMax=later,
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )

            events = events_result.get("items", [])
            if not events:
                return "No upcoming events in the next {} days.".format(days)

            lines = ["Upcoming events (next {} days):".format(days)]
            for event in events:
                summary = event.get("summary", "No title")
                start = event.get("start", {})

                if "dateTime" in start:
                    try:
                        dt = datetime.fromisoformat(
                            start["dateTime"].replace("Z", "+00:00")
                        )
                        date_str = dt.strftime("%b %d %H:%M")
                    except Exception:
                        date_str = start["dateTime"][:16]
                elif "date" in start:
                    date_str = start["date"]
                else:
                    date_str = "TBD"

                lines.append(f"  - {summary} ({date_str})")

            return "\n".join(lines)
        except Exception as e:
            return self._handle_api_error(e, "get_agenda")

    def create_event(
        self,
        summary: str,
        start_time: str,
        end_time: str,
        description: str = "",
        attendees: Optional[List[str]] = None,
    ) -> str:
        try:
            service = self._get_calendar_service()

            # Normalize datetime format: replace space with T for RFC3339
            if " " in start_time and "T" not in start_time:
                start_time = start_time.replace(" ", "T")
            if " " in end_time and "T" not in end_time:
                end_time = end_time.replace(" ", "T")

            event = {
                "summary": summary,
                "description": description,
                "start": {"dateTime": start_time, "timeZone": "Asia/Kolkata"},
                "end": {"dateTime": end_time, "timeZone": "Asia/Kolkata"},
            }

            if attendees:
                event["attendees"] = [{"email": a} for a in attendees]

            created = (
                service.events().insert(calendarId="primary", body=event).execute()
            )
            return f"Event created: {summary} at {start_time[:16]} ({created.get('htmlLink', '')})"
        except Exception as e:
            return self._handle_api_error(e, "create_event")

    def update_event(
        self,
        event_id: str,
        summary: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        description: Optional[str] = None,
    ) -> str:
        try:
            service = self._get_calendar_service()

            existing = (
                service.events()
                .get(calendarId="primary", eventId=event_id)
                .execute()
            )

            if summary:
                existing["summary"] = summary
            if description:
                existing["description"] = description
            if start_time:
                existing["start"] = {"dateTime": start_time, "timeZone": "Asia/Kolkata"}
            if end_time:
                existing["end"] = {"dateTime": end_time, "timeZone": "Asia/Kolkata"}

            updated = (
                service.events()
                .update(calendarId="primary", eventId=event_id, body=existing)
                .execute()
            )
            return f"Event updated: {updated.get('summary', event_id)}"
        except Exception as e:
            return self._handle_api_error(e, "update_event")

    def delete_event(self, event_id: str) -> str:
        try:
            service = self._get_calendar_service()
            service.events().delete(calendarId="primary", eventId=event_id).execute()
            return f"Event deleted: {event_id}"
        except Exception as e:
            return self._handle_api_error(e, "delete_event")

    def delete_event_by_query(self, query: str, days: int = 14) -> str:
        """Find an event by summary substring and delete it.

        The model never sees Google's internal event IDs (get_agenda doesn't
        expose them), so deletion has to go by name. Refuses to guess when
        the name matches more than one upcoming event rather than deleting
        all of them.
        """
        try:
            service = self._get_calendar_service()
            now = datetime.now(timezone.utc).isoformat()
            later = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

            events_result = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=now,
                    timeMax=later,
                    maxResults=50,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            events = events_result.get("items", [])
            q = query.strip().lower()
            matches = [e for e in events if q in e.get("summary", "").lower()]

            if not matches:
                return f"No event found matching '{query}' in the next {days} days."

            if len(matches) > 1:
                lines = [f"{len(matches)} events match '{query}' — say which one by date instead of the name:"]
                for e in matches:
                    start = e.get("start", {})
                    when = start.get("dateTime", start.get("date", "TBD"))
                    lines.append(f"  - {e.get('summary', 'No title')} ({when[:16]})")
                return "\n".join(lines)

            event = matches[0]
            service.events().delete(calendarId="primary", eventId=event["id"]).execute()
            start = event.get("start", {})
            when = start.get("dateTime", start.get("date", "TBD"))
            return f"Event deleted: {event.get('summary', 'No title')} ({when[:16]})"
        except Exception as e:
            return self._handle_api_error(e, "delete_event_by_query")

    # ============ GMAIL ============

    def triage_emails(self, max_results: int = 10) -> str:
        try:
            service = self._get_gmail_service()

            results = (
                service.users()
                .messages()
                .list(userId="me", maxResults=max_results, labelIds=["INBOX"])
                .execute()
            )

            messages = results.get("messages", [])
            if not messages:
                return "No new emails in inbox."

            lines = ["Recent emails ({} total):".format(len(messages))]
            for msg in messages[:10]:
                msg_data = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg["id"], format="metadata", metadataHeaders=["Subject", "From", "Date"])
                    .execute()
                )

                headers = msg_data.get("payload", {}).get("headers", [])
                subject = next(
                    (h["value"] for h in headers if h["name"] == "Subject"),
                    "No Subject",
                )
                sender = next(
                    (h["value"] for h in headers if h["name"] == "From"), "Unknown"
                )
                date = next(
                    (h["value"] for h in headers if h["name"] == "Date"), ""
                )

                snippet = msg_data.get("snippet", "")[:80]
                if "<" in sender:
                    sender = sender.split("<")[1].rstrip(">")

                lines.append(f"  - {subject[:60]}")
                lines.append(f"    From: {sender[:40]} | {date[:16]}")
                if snippet:
                    lines.append(f"    {snippet}")

            return "\n".join(lines)
        except Exception as e:
            return self._handle_api_error(e, "triage_emails")

    def read_email(self, email_id: str) -> str:
        try:
            service = self._get_gmail_service()

            msg = (
                service.users()
                .messages()
                .get(userId="me", id=email_id, format="full")
                .execute()
            )

            headers = msg.get("payload", {}).get("headers", [])
            subject = next(
                (h["value"] for h in headers if h["name"] == "Subject"), "No Subject"
            )
            sender = next(
                (h["value"] for h in headers if h["name"] == "From"), "Unknown"
            )
            date = next((h["value"] for h in headers if h["name"] == "Date"), "")

            parts = msg.get("payload", {}).get("parts", [])
            body = ""
            for part in parts:
                if part.get("mimeType") == "text/plain":
                    data = part.get("body", {}).get("data", "")
                    if data:
                        body = base64.urlsafe_b64decode(data).decode("utf-8")
                        break

            if not body and msg.get("payload", {}).get("body", {}).get("data"):
                body = base64.urlsafe_b64decode(
                    msg["payload"]["body"]["data"]
                ).decode("utf-8")

            if "<" in sender:
                sender = sender.split("<")[1].rstrip(">")

            return f"From: {sender}\nDate: {date}\nSubject: {subject}\n\n{body[:1500]}"
        except Exception as e:
            return self._handle_api_error(e, "read_email")

    def send_email(self, to: str, subject: str, body: str) -> str:
        try:
            service = self._get_gmail_service()

            message = EmailMessage()
            message.set_content(body)
            message["To"] = to
            message["From"] = "me"
            message["Subject"] = subject

            encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()

            result = (
                service.users()
                .messages()
                .send(userId="me", body={"raw": encoded})
                .execute()
            )

            return f"Email sent to {to} (ID: {result.get('id')})"
        except Exception as e:
            return self._handle_api_error(e, "send_email")

    # ============ DRIVE ============

    def list_files(
        self, query: str = "", max_results: int = 10, order_by: str = "modifiedTime desc"
    ) -> str:
        try:
            service = self._get_drive_service()

            params = {
                "pageSize": max_results,
                "fields": "files(id, name, mimeType, modifiedTime, size)",
                "orderBy": order_by,
            }
            if query:
                params["q"] = query

            results = service.files().list(**params).execute()
            files = results.get("files", [])

            if not files:
                return "No files found."

            lines = ["Files ({} total):".format(len(files))]
            for f in files[:10]:
                size = f.get("size")
                size_str = f"{int(size) / 1024:.1f} KB" if size else "N/A"
                modified = f.get("modifiedTime", "")[:16]
                lines.append(f"  - {f['name']} ({f['mimeType']})")
                lines.append(f"    Modified: {modified} | Size: {size_str}")

            return "\n".join(lines)
        except Exception as e:
            return self._handle_api_error(e, "list_files")

    def upload_file(self, file_path: str, mime_type: str = "application/octet-stream") -> str:
        try:
            from googleapiclient.http import MediaFileUpload

            service = self._get_drive_service()
            path = Path(file_path)

            if not path.exists():
                return f"File not found: {file_path}"

            file_metadata = {"name": path.name}
            media = MediaFileUpload(str(path), mimetype=mime_type)

            file = (
                service.files()
                .create(body=file_metadata, media_body=media, fields="id, name, webViewLink")
                .execute()
            )

            return f"Uploaded: {file['name']} (ID: {file['id']}, Link: {file.get('webViewLink', '')})"
        except Exception as e:
            return self._handle_api_error(e, "upload_file")

    def search_files(self, query: str, max_results: int = 10) -> str:
        try:
            service = self._get_drive_service()

            safe_query = query.replace("'", "\\'")
            results = (
                service.files()
                .list(
                    q=f"name contains '{safe_query}'",
                    pageSize=max_results,
                    fields="files(id, name, mimeType, modifiedTime)",
                )
                .execute()
            )

            files = results.get("files", [])
            if not files:
                return f"No files matching '{query}'."

            lines = [f"Search results for '{query}':"]
            for f in files:
                modified = f.get("modifiedTime", "")[:16]
                lines.append(f"  - {f['name']} ({f['mimeType']}) - Modified: {modified}")

            return "\n".join(lines)
        except Exception as e:
            return self._handle_api_error(e, "search_files")

    # ============ GENERIC FALLBACK ============

    def run_generic(self, command: str) -> str:
        """Fallback for legacy gws CLI commands. Maps old syntax to new API."""
        parts = command.lower().split()

        if not parts:
            return "Empty command. Use: calendar list, email triage, drive list, etc."

        service = parts[0] if parts else ""
        action = parts[1] if len(parts) > 1 else ""

        if service == "calendar":
            if action in ("list", "agenda", "check"):
                days = 7
                for p in parts:
                    if p.isdigit():
                        days = int(p)
                return self.get_agenda(days=days)
            elif action in ("create", "add"):
                return "Use calendar tool with create action directly"
            else:
                return self.get_agenda()
        elif service in ("email", "gmail", "mail"):
            if action in ("list", "triage", "check", "inbox"):
                return self.triage_emails()
            elif action == "send":
                return "Use email tool with send action directly"
            else:
                return self.triage_emails()
        elif service == "drive":
            if action in ("list", "files"):
                return self.list_files()
            elif action == "search":
                query = " ".join(parts[2:]) if len(parts) > 2 else ""
                return self.search_files(query)
            else:
                return self.list_files()
        elif service in ("sheets", "docs", "people", "contacts"):
            return f"{service.capitalize()} access is not yet wrapped. Please use the specific '{service}' tool once implemented, or perform this action manually."
        else:
            return f"Unknown service: {service}. Available: calendar, email, drive"

    # ============ HEALTH CHECK ============

    def health_check(self) -> str:
        try:
            self._ensure_creds(ALL_SCOPES)
            scopes_granted = self._creds.scopes or []

            status = []
            if any("calendar" in s for s in scopes_granted):
                status.append("Calendar: OK")
            else:
                status.append("Calendar: NOT GRANTED")

            if any("gmail" in s for s in scopes_granted):
                status.append("Gmail: OK")
            else:
                status.append("Gmail: NOT GRANTED")

            if any("drive" in s for s in scopes_granted):
                status.append("Drive: OK")
            else:
                status.append("Drive: NOT GRANTED")

            return "GWS Health: " + ", ".join(status)
        except Exception as e:
            return f"GWS Health: FAILED - {e}"


def run_auth_flow(scopes: List[str]):
    """Run OAuth flow manually (for --auth CLI command)."""
    if not CREDS_FILE.exists():
        raise FileNotFoundError(
            f"Credentials file not found at {CREDS_FILE}.\n"
            "Place your OAuth client credentials there first."
        )

    print("\nStarting OAuth flow with scopes:")
    for s in scopes:
        print(f"  - {s}")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), scopes)
    creds = flow.run_local_server(port=0)

    save_token(creds)

    print(f"\nToken saved to: {TOKEN_FILE}")
    print(f"Granted scopes: {creds.scopes}")
    print("\nYou can now use GWSClient for Calendar, Gmail, and Drive.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Google Workspace Client CLI")
    parser.add_argument("--auth", action="store_true", help="Run OAuth authentication flow")
    parser.add_argument(
        "--scopes",
        type=str,
        default="calendar,gmail,drive.file",
        help="Comma-separated scopes (default: calendar,gmail,drive.file)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test connection to all services",
    )

    args = parser.parse_args()

    if args.auth:
        scope_names = []
        for s in args.scopes.split(","):
            s = s.strip()
            if s in SCOPE_MAP:
                scope_names.extend(SCOPE_MAP[s])
            elif s.startswith("https://"):
                scope_names.append(s)
            else:
                print(f"Warning: Unknown scope '{s}', skipping")

        if not scope_names:
            scope_names = ALL_SCOPES

        try:
            run_auth_flow(scope_names)
        except Exception as e:
            print(f"OAuth flow failed: {e}")
            exit(1)
    elif args.test:
        client = GWSClient()
        print(client.health_check())
    else:
        parser.print_help()
