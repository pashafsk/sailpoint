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

# Storage: Postgres when DATABASE_URL is set (persists across restarts and
# redeploys), otherwise the local data.json file (for local PyCharm runs).
DATABASE_URL = os.environ.get("DATABASE_URL")
STORAGE = "postgres" if DATABASE_URL else "file"

LOCK = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def empty_data():
    return {
        "accounts": [],
        "requests": []
    }


def read_seed_file():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except json.JSONDecodeError:
            pass

    return None


# ---------- Postgres storage ----------

def pg_connect():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def pg_load():
    conn = pg_connect()

    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS mock_state ("
                " id INTEGER PRIMARY KEY,"
                " data JSONB NOT NULL,"
                " updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            )
            cur.execute("SELECT data FROM mock_state WHERE id = 1")
            row = cur.fetchone()

            if row:
                print("[STORAGE] Loaded state from Postgres", flush=True)
                return row[0]

            # First run on an empty database: import data.json if the repo
            # has one, so existing test accounts carry over.
            data = read_seed_file() or empty_data()
            cur.execute(
                "INSERT INTO mock_state (id, data) VALUES (1, %s)",
                (json.dumps(data),)
            )
            print("[STORAGE] Initialized Postgres state", flush=True)
            return data
    finally:
        conn.close()


def pg_save(data):
    conn = pg_connect()

    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mock_state (id, data, updated_at)"
                " VALUES (1, %s, now())"
                " ON CONFLICT (id) DO UPDATE"
                " SET data = EXCLUDED.data, updated_at = now()",
                (json.dumps(data),)
            )
    finally:
        conn.close()


# ---------- File storage ----------

def file_load():
    data = read_seed_file()

    if data is not None:
        return data

    data = empty_data()
    DATA_FILE.write_text(json.dumps(data, indent=2))
    return data


def file_save(data):
    temp_file = DATA_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(data, indent=2))
    temp_file.replace(DATA_FILE)


# ---------- Public API used by the handlers ----------

def save_data():
    if STORAGE == "postgres":
        pg_save(database)
    else:
        file_save(database)


def load_data():
    if STORAGE == "postgres":
        return pg_load()

    return file_load()


database = load_data()


def find_account(identifier):
    identifier = unquote(identifier)

    for account in database["accounts"]:
        if account.get("accountId") == identifier:
            return account

        if account.get("email", "").lower() == identifier.lower():
            return account

    return None


def account_records():
    records = []

    for account in database["accounts"]:
        records.append({
            "accountId": account.get("accountId"),
            "email": account.get("email"),
            "displayName": account.get("displayName", ""),
            "entitlementIds": [
                item.get("entitlementId")
                for item in account.get("entitlements", [])
                if item.get("status", "ACTIVE") == "ACTIVE"
            ],
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


# Entitlement catalog returned to SailPoint Group Aggregation.
# The id is the exact entitlement value SailPoint stores and sends
# on provisioning, so keep these strings stable once aggregated.
ENTITLEMENTS = [
    "MDA_DEVELOPER_CORE",
    "MDA_DEVELOPER_CDBSLA",
    "Clinical Data Base (CDB) | CaseProfileReader",
    "Clinical Data Base (CDB) | CDBDashboardReader",
    "Clinical Data Base (CDB) | CDBDownloads",
    "Clinical Data Base (CDB) | CDBReader",
    "Clinical Data Base (CDB) | CRMDownloads",
    "Clinical Data Base (CDB) | CRMReader",
    "Clinical Data Base (CDB) | ExpressionWriter",
    "Clinical Data Base (CDB) | OPPEReader",
    "Clinical Data Base (CDB) | PhysicianDataReader",
    "Clinical Data Base (CDB) | PhysicianProfile",
    "Clinical Practice Solution Center (CPSC) |Events Calendar",
    "Clinical Practice Solution Center (CPSC) |Full Access",
    "Clinical Practice Solution Center (CPSC) |IA Cognos ReportNet Server (PRO-IA-CognosReportNet)",
    "Clinical Practice Solution Center (CPSC) |Member Directory",
    "Clinical Practice Solution Center (CPSC) |Member participation",
    "Clinical Practice Solution Center (CPSC) |Opportunity Dashboard (PRO -FPSC-ALL)",
    "Clinical Practice Solution Center (CPSC) |PRO-FPSC-ALL",
    "Clinical Practice Solution Center (CPSC) |Staff Directory",
    "Operational Data Base (ODB) |ODB Reports: Administration",
    "Operational Data Base (ODB) |ODB Reports: Advanced Reports",
    "Operational Data Base (ODB) |ODB Reports: Client Indicators",
    "Operational Data Base (ODB) |ODB Reports: Compare Groups",
    "Operational Data Base (ODB) |ODB Reports: Department Reports",
    "Operational Data Base (ODB) |ODB Reports: Executive Reports",
    "Operational Data Base (ODB) |ODB Reports: Generic Reports",
    "Operational Data Base (ODB) |ODB Reports: Internal Administration (Internal Use)",
    "Operational Data Base (ODB) |ODB Reports: IOA Report",
    "Operational Data Base (ODB) |ODB Reports: Item Sharing",
    "Operational Data Base (ODB) |ODB Reports: Managers",
    "Operational Data Base (ODB) |ODB Reports: User Impersonation",
    "Operational Data Base (ODB) |ODB SEC: DRM",
    "Operational Data Base (ODB) |ODB SEC: Support",
    "Operational Data Base (ODB) |ODB Security: (Must For All ODB Security Users)",
    "Operational Dashboard |Operational Dashboard : ODB Product Support Staff",
    "Procedural Analytics - Data Download",
    "Staff Access-PHI",
    "Performance Dashboard |ODB Product Support Staff",
    "AQA Calculator",
]


def entitlement_records():
    records = []

    for value in ENTITLEMENTS:
        application = value.split("|")[0].strip() if "|" in value else value
        records.append({
            "id": value,
            "name": value,
            "description": application
        })

    return records


class WebServiceHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(format % args, flush=True)

    def send_json(self, status_code, data):
        response = json.dumps(data).encode("utf-8")

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()

        self.wfile.write(response)

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)

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
                    "service": "sailpoint-stateful-mock",
                    "storage": STORAGE,
                    "accounts": len(database["accounts"])
                })

            # Group/entitlement aggregation
            if request_path == "/entitlements":
                records = entitlement_records()

                return self.send_json(200, {
                    "value": records,
                    "Count": len(records)
                })

            # SailPoint Account Aggregation
            # Root Path in SailPoint: $.accounts
            if request_path == "/aggregation/accounts":
                records = account_records()
                print(
                    f"[AGGREGATION] {now()} "
                    f"client={self.client_address[0]} "
                    f"user_agent={self.headers.get('User-Agent')} "
                    f"accounts_returned={len(records)} "
                    f"body={json.dumps({'accounts': records})}",
                    flush=True
                )
                return self.send_json(200, {
                    "accounts": records
                })

            # Other account listings (unchanged: top-level array)
            if request_path in ("/aggregation", "/accounts"):
                return self.send_json(200, account_records())

            if len(parts) == 2 and parts[0] == "accounts":
                account = find_account(parts[1])

                if not account:
                    return self.send_json(404, {
                        "error": "account_not_found"
                    })

                return self.send_json(200, account)

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
                    database.update({
                        "accounts": [],
                        "requests": []
                    })
                    save_data()

                return self.send_json(200, {
                    "success": True
                })

            if (
                len(parts) == 3
                and parts[0] == "accounts"
                and parts[2] == "entitlements"
            ):
                body = self.read_body()

                account_id = parts[1]

                # Email is optional: SailPoint identifies the account by the
                # accountId in the URL ($plan.nativeIdentity$).
                email = (
                    body.get("email")
                    or body.get("username")
                    or body.get("identity")
                    or ""
                )

                raw_entitlement = (
                    body.get("entitlementId")
                    or body.get("entitlement_id")
                    or body.get("id")
                )

                # SailPoint may send one value or a list of values.
                if isinstance(raw_entitlement, list):
                    entitlement_ids = [str(v) for v in raw_entitlement if v]
                elif raw_entitlement:
                    entitlement_ids = [str(raw_entitlement)]
                else:
                    entitlement_ids = []

                operation = str(
                    body.get("operation", "ADD")
                ).upper()

                if not entitlement_ids:
                    return self.send_json(400, {
                        "error": "entitlementId is required"
                    })

                entitlement_id = (
                    entitlement_ids[0]
                    if len(entitlement_ids) == 1
                    else entitlement_ids
                )

                with LOCK:
                    account = find_account(account_id)

                    if not account and email:
                        account = find_account(email)

                    if not account:
                        account = {
                            "accountId": account_id,
                            "email": email,
                            "displayName": body.get("displayName", ""),
                            "entitlements": []
                        }

                        database["accounts"].append(account)

                    if email and not account.get("email"):
                        account["email"] = email

                    email = account.get("email", "")
                    timestamp = now()

                    for value in entitlement_ids:
                        if operation in ("REMOVE", "DELETE"):
                            account["entitlements"] = [
                                item
                                for item in account["entitlements"]
                                if item.get("entitlementId") != value
                            ]

                        else:
                            exists = any(
                                item.get("entitlementId") == value
                                for item in account["entitlements"]
                            )

                            if not exists:
                                account["entitlements"].append({
                                    "entitlementId": value,
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
                    save_data()

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
