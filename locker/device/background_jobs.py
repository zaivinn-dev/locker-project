"""Background jobs for locker system (cleanup, notifications, etc)."""
import threading
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
from db import connect


class BackgroundJobScheduler:
    """Manages background tasks for guest card lifecycle."""
    
    def __init__(self):
        self.running = False
        self.thread = None
    
    def start(self):
        """Start the background job scheduler."""
        if self.running:
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        print("Background job scheduler started")
    
    def stop(self):
        """Stop the background job scheduler."""
        self.running = False
    
    def _run_loop(self):
        """Main loop for background jobs."""
        while self.running:
            try:
                # No automatic guest expiry notifications: guest RFID cards no longer auto-expire.
                # Background jobs remain idle unless additional tasks are added.
                import time
                time.sleep(3600)
            except Exception as e:
                print(f"[BACKGROUND JOB ERROR] {e}")
                import traceback
                traceback.print_exc()
    
    def cleanup_expired_cards(self):
        """Deprecated cleanup method; guest access no longer auto-expires."""
        return
    
    def send_overdue_notifications(self):
        """Send email notifications for overdue card returns."""
        from datetime import datetime, timedelta
        
        with connect() as conn:
            # Find overdue cards
            overdue = conn.execute(
                """SELECT grc.*, m.full_name, m.contact_number 
                   FROM guest_rfid_cards grc
                   JOIN members m ON grc.guest_id = m.id
                   WHERE grc.status = 'ACTIVE'
                   AND grc.expected_return_time < datetime('now')
                   AND grc.expected_return_time > datetime('now', '-1 day')""",  # Within last 24h (send once)
            ).fetchall()
            
            for card in overdue:
                try:
                    send_overdue_card_email(dict(card))
                    print(f"[EMAIL] Sent overdue notification to {card['full_name']}")
                except Exception as e:
                    print(f"[EMAIL ERROR] Failed to send overdue notification: {e}")


def send_overdue_card_email(card_data):
    """Send email notification for overdue card return."""
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    sender_email = os.getenv("SMTP_FROM_EMAIL")
    sender_password = os.getenv("SMTP_FROM_PASSWORD")
    
    if not sender_email or not sender_password:
        print("[EMAIL] SMTP credentials not configured")
        return
    
    guest_email = card_data.get("contact_number") or ""  # Could store actual email
    if not guest_email or "@" not in guest_email:
        return  # Skip if no valid email
    
    # Create email
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "⚠️ URGENT: Guest Card Return Overdue"
    msg["From"] = sender_email
    msg["To"] = guest_email
    
    expire_time = datetime.fromisoformat(card_data["expected_return_time"])
    hours_overdue = int((datetime.now() - expire_time).total_seconds() / 3600)
    
    text = f"""
Dear {card_data['full_name']},

Your locker access card (RFID UID: {card_data['rfid_uid']}) is OVERDUE for return.

Details:
- Expected return: {expire_time.strftime('%Y-%m-%d %H:%M')}
- Hours overdue: {hours_overdue}
- Card status: ACTIVE (will auto-expire in 24 hours)

IMPORTANT: If you do not return the card within 24 hours, it will be automatically blacklisted 
and you may be charged a replacement fee.

Please return your card to the admin desk immediately.

Questions? Contact admin.
"""
    
    html = f"""
    <html>
      <body>
        <h2>⚠️ Card Return Overdue</h2>
        <p>Dear {card_data['full_name']},</p>
        <p>Your locker access card is <strong>OVERDUE for return</strong>.</p>
        
        <table style="border-collapse: collapse; width: 100%;">
          <tr style="background: #f8f9fa;">
            <td style="padding: 8px;">Card UID:</td>
            <td style="padding: 8px;"><code>{card_data['rfid_uid']}</code></td>
          </tr>
          <tr>
            <td style="padding: 8px;">Due:</td>
            <td style="padding: 8px;">{expire_time.strftime('%Y-%m-%d %H:%M')}</td>
          </tr>
          <tr style="background: #f8f9fa;">
            <td style="padding: 8px;">Overdue by:</td>
            <td style="padding: 8px; color: #dc3545;"><strong>{hours_overdue} hours</strong></td>
          </tr>
        </table>
        
        <p style="color: #dc3545; font-weight: bold;">
          ⚠️ If not returned within 24 hours, the card will be auto-blacklisted 
          and a replacement fee may apply.
        </p>
        
        <p>Please return your card to the admin desk immediately.</p>
      </body>
    </html>
    """
    
    part1 = MIMEText(text, "plain")
    part2 = MIMEText(html, "html")
    msg.attach(part1)
    msg.attach(part2)
    
    # Send email
    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, guest_email, msg.as_string())
        server.quit()
    except Exception as e:
        print(f"[SMTP ERROR] {e}")
        raise


def add_card_fee_to_payment(guest_id: int, reason: str = "Lost RFID card replacement"):
    """Add a card fee to the guest's account."""
    card_fee = float(os.getenv("CARD_REPLACEMENT_FEE", "50.0"))
    
    with connect() as conn:
        conn.execute(
            """INSERT INTO payments (member_id, amount, payment_type, notes)
               VALUES (?, ?, ?, ?)""",
            (guest_id, card_fee, "card_fee", reason),
        )


# Global job scheduler instance
_job_scheduler = BackgroundJobScheduler()

def start_background_jobs():
    """Start all background jobs."""
    _job_scheduler.start()

def stop_background_jobs():
    """Stop all background jobs."""
    _job_scheduler.stop()
