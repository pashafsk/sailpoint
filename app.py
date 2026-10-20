#!/usr/bin/env python3
"""Stateful mock Web Service for SailPoint entitlement requests."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "3000"))
API_KEY = os.getenv("API_KEY", "")
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))
LOCK = threading.Lock()

DEFAULT_DB = {
    "entitlements": [
        {"id": "MDA_DEVELOPER_CORE", "name": "MDA Developer Core",
         "description": "Grants access to the MDA environment for Developers"},
        {"id": "MDA_DEVELOPER_CDBSLA", "name": "MDA Developer CDBSLA",
         "description": "Grants access to the MDA CDBSLA environment for Developers"},
    ],
    "accounts": [],
    "requests": [],
}


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_db():
    try:
        return json.loads(DATA_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        db = json.loads(json.dumps(DEFAULT_DB))
        save_db(db)
        return db


def save_db(db):
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(db, indent=2))
    temporary.replace(DATA_FILE)


DB = load_db()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        if not API_KEY:
            return True
        supplied = self.headers.get("Authorization", "")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:]
        if supplied != API_KEY:
            self.send_json(401, {"error": "unauthorized"})
            return False
        return True

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON") from exc

    def do_GET(self):
        if not self.authorized():
            return
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        with LOCK:
            if self.path == "/health":
                return self.send_json(200, {"status": "ok", "service": "sailpoint-stateful-mock"})
            if self.path == "/entitlements":
                return self.send_json(200, DB["entitlements"])
            if self.path == "/accounts":
                return self.send_json(200, DB["accounts"])
            if len(parts) == 2 and parts[0] == "accounts":
                account = next((a for a in DB["accounts"] if a["accountId"] == parts[1]), None)
                return self.send_json(200 if account else 404, account or {"error": "account_not_found"})
            if len(parts) == 3 and parts[0] == "accounts" and parts[2] == "entitlements":
                account = next((a for a in DB["accounts"] if a["accountId"] == parts[1]), None)
                return self.send_json(200 if account else 404, account["entitlements"] if account else {"error": "account_not_found"})
            if len(parts) == 2 and parts[0] == "requests":
                request = next((r for r in DB["requests"] if r["requestId"] == parts[1]), None)
                return self.send_json(200 if request else 404, request or {"error": "request_not_found"})
        self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        if not self.authorized():
            return
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        try:
            if self.path == "/reset":
                with LOCK:
                    DB.clear(); DB.update(json.loads(json.dumps(DEFAULT_DB))); save_db(DB)
                return self.send_json(200, {"success": True})
            if len(parts) == 3 and parts[0] == "accounts" and parts[2] == "entitlements":
                body = self.body()
                account_id = parts[1]
                entitlement_id = body.get("entitlementId") or body.get("entitlement_id") or body.get("id")
                if not entitlement_id:
                    return self.send_json(400, {"error": "entitlementId is required"})
                operation = str(body.get("operation", body.get("action", "ADD"))).upper()
                with LOCK:
                    account = next((a for a in DB["accounts"] if a["accountId"] == account_id), None)
                    if not account:
                        account = {"accountId": account_id, "email": body.get("email", body.get("username", "")),
                                   "displayName": body.get("displayName", ""), "entitlements": []}
                        DB["accounts"].append(account)
                    stamp = now()
                    request = {"requestId": f"req-{uuid.uuid4()}", "accountId": account_id,
                               "entitlementId": entitlement_id, "operation": operation,
                               "status": "COMPLETED", "requestedAt": stamp, "completedAt": stamp}
                    if operation in ("REMOVE", "DELETE"):
                        account["entitlements"] = [e for e in account["entitlements"] if e["entitlementId"] != entitlement_id]
                    elif not any(e["entitlementId"] == entitlement_id for e in account["entitlements"]):
                        account["entitlements"].append({"entitlementId": entitlement_id, "status": "ACTIVE", "source": "mock", "addedAt": stamp})
                    DB["requests"].append(request); save_db(DB)
                return self.send_json(200, {"success": True, "status": "COMPLETED", **{k: request[k] for k in ("requestId", "accountId", "entitlementId")}, "message": "Entitlement request completed successfully"})
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        self.send_json(404, {"error": "not_found"})


if __name__ == "__main__":
    print(f"Stateful mock Web Service listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
