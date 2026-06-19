import os, json, sys, urllib.request, urllib.error

payload = json.load(open(sys.argv[1]))

body = json.dumps({
    "from": os.environ.get("EMAIL_FROM", "onboarding@resend.dev"),
    "to": [payload.get("to", "dr.mario.grandi@gmail.com")],
    "subject": payload["subject"],
    "html": payload["html_body"],
}).encode()

req = urllib.request.Request(
    "https://api.resend.com/emails",
    data=body,
    headers={
        "Authorization": "Bearer " + os.environ["RESEND_API_KEY"],
        "Content-Type": "application/json",
        "User-Agent": "curl/8.0",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.load(resp)
    print(f"Email sent — id: {result.get('id')}")
except urllib.error.HTTPError as e:
    print(f"Send failed: {e.code} {e.read().decode()}")
    sys.exit(1)
except urllib.error.URLError as e:
    print(f"Connection failed: {e.reason}")
    sys.exit(1)