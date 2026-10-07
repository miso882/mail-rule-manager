"""Gmail authentication, request pacing, and temporary-error retries."""
import json
import random
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from services.config_service import gmail_file
from services.storage_service import write_private_text

ROOT = Path(__file__).resolve().parent.parent
SCOPES = ["https://mail.google.com/"]


def connect_gmail():
    token = gmail_file("GMAIL_TOKEN_FILE", "token.json", ROOT)
    client_file = gmail_file("GMAIL_CREDENTIALS_FILE", "credentials.json", ROOT)
    for private_file in (token, client_file):
        if private_file.exists(): private_file.chmod(0o600)
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client_file.exists():
                raise FileNotFoundError("Place credentials.json beside main.py, or set GMAIL_CREDENTIALS_FILE in .env.")
            flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
            creds = flow.run_local_server(port=0, prompt="consent")
        write_private_text(token, creds.to_json())
    service = build("gmail", "v1", credentials=creds)
    service._http.timeout = 30
    return service


class Gmail:
    provider = "gmail"
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def call(self, request):
        retries = self.settings["retry_attempts"]
        for attempt in range(retries):
            time.sleep(self.settings["request_delay"])
            try:
                return request.execute()
            except HttpError as error:
                try:
                    data = json.loads(error.content.decode())
                    reasons = {e.get("reason") for e in data.get("error", {}).get("errors", [])}
                except (ValueError, AttributeError):
                    reasons = set()
                temporary = error.resp.status in {429, 500, 502, 503, 504} or (
                    error.resp.status == 403 and bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded"})
                )
                if not temporary or attempt == retries - 1:
                    raise
            except OSError:
                if attempt == retries - 1:
                    raise
            delay = min(60, 2 ** (attempt + 1)) + random.random()
            print(f"Temporary Gmail/network limit. Retrying in {delay:.1f}s...", flush=True)
            time.sleep(delay)

    def account(self):
        return self.call(self.api.users().getProfile(userId="me"))["emailAddress"]

    def close(self):
        self.api.close()
