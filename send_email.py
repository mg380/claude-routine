import smtplib, ssl, os, json, sys
from email.message import EmailMessage

payload = json.load(open(sys.argv[1]))

msg = EmailMessage()
msg["From"] = os.environ["GMAIL_SENDER"]
msg["To"] = payload.get("to", "dr.mario.grandi@gmail.com")
msg["Subject"] = payload["subject"]
msg.set_content("This email contains HTML; view in an HTML-capable client.")
msg.add_alternative(payload["html_body"], subtype="html")

ctx = ssl.create_default_context()
with smtplib.SMTP("smtp.gmail.com", 587) as s:
    s.starttls(context=ctx)
    s.login(os.environ["GMAIL_SENDER"], os.environ["GMAIL_APP_PASSWORD"])
    s.send_message(msg)

print(f"Email sent to {msg['To']} — subject: {payload['subject']}")