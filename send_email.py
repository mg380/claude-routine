import os, ssl, smtplib
from email.message import EmailMessage
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("email-sender")

@mcp.tool()
def send_email(subject: str, html_body: str, to: str = "dr.mario.grandi@gmail.com") -> str:
    """Send an email. Provide the subject line, the full HTML body, and
    optionally the recipient (defaults to dr.mario.grandi@gmail.com)."""
    msg = EmailMessage()
    msg["From"] = os.environ["GMAIL_SENDER"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("This email contains HTML; view in an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls(context=ctx)
        s.login(os.environ["GMAIL_SENDER"], os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)
    return f"Email sent to {to} — subject: {subject}"

if __name__ == "__main__":
    mcp.run()