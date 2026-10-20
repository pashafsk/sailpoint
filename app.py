import json
import os
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "3000"))
DATA_FILE = Path(os.environ.get("DATA_FILE", "data.json"))

LOCK = threading.Lock()

DEFAULT_DB = {
    "accounts": [],
    "requests": []
}


def current_time():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def save_database():
    temp_file = DATA_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(database, indent=2))
    temp_file.replace(DATA_FILE)


def load_database():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except json.JSONDecodeError:
            pass

    DATA_FILE.write_text(json.dumps(DEFAULT_DB, indent=2))
    return json.loads(json.dumps(DEFAULT_DB))


database = load_database()


def find_account(identifier):
    identifier = unquote(identifier)

    for account in database["accounts"]:
        if account.get("accountId") == identifier:
            return account

        if account.get("email", "").lower() == identifier.lower():
            return account

    return None


def aggregation_records():
    records = []

    for account in database["accounts"]:
        records.append({
            "accountId": account.get("accountId"),
            "email": account.get("email"),
            "displayName": account.get("displayName", ""),
            "entitlements": [
                {
                    "entitlementId": item.get("entitlementId"),
                    "status": item.get("status", "ACTIVE")
                }
                for item in account.get("entitlements", [])
                if item.get("status", "ACTIVE") == "ACTIVE"
            ]
        })

    return records


class WebServiceHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(format % args)

    def send_json(self, status_code, data):
        response = json.dumps(data).encode("utf-8")

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()

        self.wfile.write(response)

    def read_body(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)

        if not body:
            return {}

        return json.loads(body.decode("utf-8"))

    def do_GET(self):
        request_path = urlparse(self.path).path

        parts = [
            unquote(part)
            for part in request_path.split("/")
            if part
        ]

        with LOCK:

            if request_path == "/health":
                return self.send_json(200, {
                    "status": "ok",
                    "service": "sailpoint-stateful-mock"
                })

            # Entitlement aggregation
            if request_path == "/entitlements":
                return self.send_json(200, [
                    {
                        "id": "MDA_DEVELOPER_CORE",
                        "name": "MDA Developer Core",
                        "description": "Mock entitlement"
                    },
                    {
                        "id": "MDA_DEVELOPER_CDBSLA",
                        "name": "MDA Developer CDBSLA",
                        "description": "Mock entitlement"
                    }
                ])

            # Account aggregation
            if request_path in (
                "/aggregation/accounts",
                "/aggregation",
                "/accounts"
            ):
                return self.send_json(200, aggregation_records())

            # Single account lookup
            if len(parts) == 2 and parts[0] == "accounts":
                account = find_account(parts[1])

                if not account:
                    return self.send_json(404, {
                        "error": "account_not_found"
                    })

                return self.send_json(200, account)

            # Single account entitlement lookup
            if (
                len(parts) == 3
                and parts[0] == "accounts"
                and parts[2] == "entitlements"
            ):
                account = find_account(parts[1])

                if not account:
                    return self.send_json(404, {
                        "error": "account_not_found"
                    })

                return self.send_json(200, {
                    "accountId": account.get("accountId"),
                    "email": account.get("email"),
                    "entitlements": account.get("entitlements", [])
                })

            # Request status lookup
            if len(parts) == 2 and parts[0] == "requests":
                request_id = parts[1]

                request_record = next(
                    (
                        item for item in database["requests"]
                        if item.get("requestId") == request_id
                    ),
                    None
                )

                if not request_record:
                    return self.send_json(404, {
                        "error": "request_not_found"
                    })

                return self.send_json(200, request_record)

        return self.send_json(404, {
            "error": "not_found",
            "path": request_path
        })

    def do_POST(self):
        request_path = urlparse(self.path).path

        parts = [
            unquote(part)
            for part in request_path.split("/")
            if part
        ]

        try:

            if request_path == "/reset":
                with LOCK:
                    database.clear()
                    database.update(json.loads(json.dumps(DEFAULT_DB)))
                    save_database()

                return self.send_json(200, {
                    "success": True
                })

            # Add or remove entitlement
            if (
                len(parts) == 3
                and parts[0] == "accounts"
                and parts[2] == "entitlements"
            ):
                body = self.read_body()

                account_id = parts[1]

                email = (
                    body.get("email")
                    or body.get("username")
                    or body.get("identity")
                )

                entitlement_id = (
                    body.get("entitlementId")
                    or body.get("entitlement_id")
                    or body.get("id")
                )

                operation = str(
                    body.get("operation", "ADD")
                ).upper()

                if not email:
                    return self.send_json(400, {
                        "error": "email is required"
                    })

                if not entitlement_id:
                    return self.send_json(400, {
                        "error": "entitlementId is required"
                    })

                with LOCK:
                    account = find_account(email)

                    if not account:
                        account = {
                            "accountId": account_id,
                            "email": email,
                            "displayName": body.get("displayName", ""),
                            "entitlements": []
                        }

                        database["accounts"].append(account)

                    timestamp = current_time()

                    if operation in ("REMOVE", "DELETE"):
                        account["entitlements"] = [
                            item
                            for item in account["entitlements"]
                            if item.get("entitlementId") != entitlement_id
                        ]

                    else:
                        exists = any(
                            item.get("entitlementId") == entitlement_id
                            for item in account["entitlements"]
                        )

                        if not exists:
                            account["entitlements"].append({
                                "entitlementId": entitlement_id,
                                "status": "ACTIVE",
                                "addedAt": timestamp
                            })

                    request_id = f"req-{uuid.uuid4()}"

                    request_record = {
                        "requestId": request_id,
                        "accountId": account_id,
                        "email": email,
                        "entitlementId": entitlement_id,
                        "operation": operation,
                        "status": "COMPLETED",
                        "requestedAt": timestamp,
                        "completedAt": timestamp
                    }

                    database["requests"].append(request_record)
                    save_database()

                return self.send_json(200, {
                    "success": True,
                    "status": "COMPLETED",
                    "requestId": request_id,
                    "accountId": account_id,
                    "email": email,
                    "entitlementId": entitlement_id
                })

        except Exception as error:
            return self.send_json(400, {
                "error": str(error)
            })

        return self.send_json(404, {
            "error": "not_found",
            "path": request_path
        })


if __name__ == "__main__":
    print(f"Service running on port {PORT}")

    server = ThreadingHTTPServer(
        (HOST, PORT),
        WebServiceHandler
    )

    server.serve_forever()
