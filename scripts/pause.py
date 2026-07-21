import httpx
r = httpx.post(
    "https://njdjclywpwrru9fnz5ptuz5u.51.75.203.30.sslip.io/admin/control/pause",
    headers={
        "x-Operator-Id": "admin",
        "idempotency-key": "pause-20260719-001-exemple",
        "Authorization": "Bearer key-xxxxxxx-exemple",

    },
)
print(r.json())