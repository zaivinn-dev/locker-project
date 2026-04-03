"""Admin routes for locker management system."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from flask import Blueprint, redirect, render_template, request, session, url_for
import json
import os

from locker.db import connect
from locker.device import get_device_controller

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

# Settings file path
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")

# Default settings
DEFAULT_SETTINGS = {
    "system_name": "Smart Locker",
    "timezone": "Asia/Manila",
    "currency": "₱",
    "language": "en",
    "membership_fee": 400,
    "membership_duration": 30,
    "renewal_fee": 400,
    "renewal_duration": 30,
    "default_guest_duration": 24,
    "max_guest_duration": 72,
    "guest_price_per_hour": 50,
    "facility_name": "Smart Locker System",
    "facility_address": "123 Main Street",
    "facility_phone": "+63 (0) 000-0000",
    "facility_email": "info@smartlocker.com",
    "unlock_duration": 5,
    "rfid_timeout": 10,
    "fingerprint_timeout": 10,
    "max_failed_attempts": 3,
    "session_timeout": 30,
    "password_min_length": 8,
    "login_attempts": 5,
    "email_notifications": False,
    "sms_notifications": False,
    "low_balance_alert": True,
    "backup_interval": 24,
    "log_retention": 90,
    "restart_time": "03:00",
    "payment_gateway": "none",
}

def load_settings():
    """Load settings from JSON file or return defaults."""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                saved = json.load(f)
                # Merge with defaults to ensure all keys exist
                return {**DEFAULT_SETTINGS, **saved}
        except (json.JSONDecodeError, IOError):
            return DEFAULT_SETTINGS.copy()
    return DEFAULT_SETTINGS.copy()

def save_settings(settings_data):
    """Save settings to JSON file."""
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings_data, f, indent=2)
        return True
    except IOError:
        return False
device = get_device_controller()


def _require_admin():
    """Check if user is logged in as admin. Redirect if not."""
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.admin_login"))
    return None


# Protect all admin routes except login
@admin_bp.before_request
def _protect_admin_routes():
    """Ensure only authenticated admins access admin panel."""
    # Allow access to login routes without authentication
    if request.endpoint in ("admin.admin_login", "admin.admin_login_submit"):
        return None
    
    # All other admin routes require authentication
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.admin_login"))



@admin_bp.get("/login")
def admin_login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin.admin_dashboard"))
    return render_template("admin/auth/login.html")


@admin_bp.post("/login")
def admin_login_submit():
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()

    # Starter hard-coded admin account. Later you can move this to a DB table.
    if username == "admin" and password == "admin123":
        session["admin_logged_in"] = True
        session["admin_username"] = username
        return redirect(url_for("admin.admin_dashboard"))

    return render_template(
        "admin/auth/login.html",
        error="Invalid username or password.",
        last_username=username,
    )


@admin_bp.get("/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin.admin_login"))


@admin_bp.get("")
def admin_dashboard():
    guard = _require_admin()
    if guard is not None:
        return guard
    with connect() as conn:
        # Auto-delete expired guests to free their RFID cards and lockers for reuse
        expired_guests = conn.execute(
            """
            SELECT id, full_name, rfid_uid, expiry_date, locker_id
            FROM members 
            WHERE member_type = 'guest' AND expiry_date < datetime('now')
            """
        ).fetchall()
        
        # Delete expired guests and log each deletion
        for guest in expired_guests:
            # Free the locker if assigned
            if guest["locker_id"]:
                conn.execute("UPDATE lockers SET status = 'available' WHERE id = ?", (guest["locker_id"],))
            
            conn.execute("DELETE FROM members WHERE id = ?", (guest["id"],))
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("system", str(guest["id"]), "guest_auto_deleted", f"rfid={guest['rfid_uid']} freed for reuse (expired at {guest['expiry_date']})"),
            )
        
        if expired_guests:
            conn.commit()
        monthly_fee = 400  # ₱400 per confirmed member (1 month)
        totals = {
            "members_total": conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'regular'").fetchone()["c"],
            "members_pending": conn.execute(
                "SELECT COUNT(*) AS c FROM members WHERE member_type = 'regular' AND (status = 'pending' OR payment_status != 'paid')"
            ).fetchone()["c"],
            "members_confirmed": conn.execute(
                "SELECT COUNT(*) AS c FROM members WHERE member_type = 'regular' AND status = 'approved' AND payment_status = 'paid'"
            ).fetchone()["c"],
            "guests_active": conn.execute(
                "SELECT COUNT(*) AS c FROM members WHERE member_type = 'guest' AND expiry_date > datetime('now')"
            ).fetchone()["c"],
            "lockers_total": conn.execute("SELECT COUNT(*) AS c FROM lockers").fetchone()["c"],
            "lockers_assigned": conn.execute(
                "SELECT COUNT(*) AS c FROM members WHERE locker_id IS NOT NULL AND (member_type != 'guest' OR expiry_date >= datetime('now'))"
            ).fetchone()["c"],
            "logs_total": conn.execute("SELECT COUNT(*) AS c FROM access_logs").fetchone()["c"],
        }

        # Calculate total revenue as 400 per confirmed member (monthly target)
        # This avoids historical duplicate renewals inflating the dashboard KPI.
        totals["revenue_total"] = totals["members_confirmed"] * monthly_fee

        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM members WHERE member_type = 'regular' GROUP BY status ORDER BY c DESC"
        ).fetchall()
        member_status_counts = {r["status"]: r["c"] for r in status_rows}

        # Activity for last 7 days (including today), based on access_logs.
        today = date.today()
        start_day = today - timedelta(days=6)
        rows = conn.execute(
            """
            SELECT date(created_at) AS d, COUNT(*) AS c
            FROM access_logs
            WHERE datetime(created_at) >= datetime('now', '-6 days')
            GROUP BY date(created_at)
            ORDER BY d ASC
            """
        ).fetchall()
        by_day = {r["d"]: r["c"] for r in rows}
        activity_7d = []
        for i in range(7):
            d = start_day + timedelta(days=i)
            key = d.isoformat()
            activity_7d.append(
                {
                    "date": key,
                    "label": d.strftime("%a"),
                    "count": int(by_day.get(key, 0)),
                }
            )

        members = conn.execute(
            "SELECT id, full_name, status, created_at FROM members WHERE member_type = 'regular' ORDER BY id DESC LIMIT 20"
        ).fetchall()
        # Show only relevant activity (member access, not system/IR sensor logs)
        logs = conn.execute(
            """
            SELECT id, actor_type, actor_ref, action, detail, created_at 
            FROM access_logs 
            WHERE actor_type NOT IN ('system', 'ir_sensor')
            ORDER BY id DESC LIMIT 20
            """
        ).fetchall()

    locker_1 = device.get_locker(1)
    return render_template(
        "admin/pages/admin_dashboard.html",
        members=members,
        logs=logs,
        locker_1=locker_1,
        current_page="dashboard",
        admin_username=session.get("admin_username"),
        totals=totals,
        member_status_counts=member_status_counts,
        activity_7d=activity_7d,
    )


@admin_bp.get("/members")
def admin_members():
    guard = _require_admin()
    if guard is not None:
        return guard
    q = (request.args.get("q") or "").strip()
    with connect() as conn:
        # Auto-delete expired guests to free their RFID cards and lockers for reuse
        expired_guests = conn.execute(
            """
            SELECT id, full_name, rfid_uid, expiry_date, locker_id
            FROM members 
            WHERE member_type = 'guest' AND expiry_date < datetime('now')
            """
        ).fetchall()
        
        # Delete expired guests and log each deletion
        for guest in expired_guests:
            # Free the locker if assigned
            if guest["locker_id"]:
                conn.execute("UPDATE lockers SET status = 'available' WHERE id = ?", (guest["locker_id"],))
            
            conn.execute("DELETE FROM members WHERE id = ?", (guest["id"],))
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("system", str(guest["id"]), "guest_auto_deleted", f"rfid={guest['rfid_uid']} freed for reuse (expired at {guest['expiry_date']})"),
            )
        
        if expired_guests:
            conn.commit()
        if q:
            members = conn.execute(
                """
                SELECT id, full_name, address, contact_number, age, category, locker_id, payment_status, paid_at, status, created_at, expiry_date
                FROM members
                WHERE status = 'approved' AND payment_status = 'paid' AND (member_type = 'regular' OR member_type IS NULL)
                  AND (full_name LIKE ? OR contact_number LIKE ? OR address LIKE ?)
                ORDER BY id DESC
                LIMIT 200
                """,
                (f"%{q}%", f"%{q}%", f"%{q}%"),
            ).fetchall()
        else:
            members = conn.execute(
                """
                SELECT id, full_name, address, contact_number, age, category, locker_id, payment_status, paid_at, status, created_at, expiry_date
                FROM members
                WHERE status = 'approved' AND payment_status = 'paid' AND (member_type = 'regular' OR member_type IS NULL)
                ORDER BY id DESC
                LIMIT 200
                """
            ).fetchall()

    return render_template(
        "admin/pages/admin_members.html",
        members=members,
        q=q,
        current_page="members",
        admin_username=session.get("admin_username"),
    )


@admin_bp.get("/pending")
def admin_pending():
    guard = _require_admin()
    if guard is not None:
        return guard
    with connect() as conn:
        pending = conn.execute(
            """
            SELECT id, full_name, category, locker_id, payment_status, status, created_at
            FROM members
            WHERE status != 'approved' OR payment_status != 'paid'
            ORDER BY id DESC
            LIMIT 200
            """
        ).fetchall()

    return render_template(
        "admin/pages/admin_pending.html",
        pending=pending,
        current_page="pending",
        admin_username=session.get("admin_username"),
    )


@admin_bp.post("/members/<int:member_id>/approve")
def admin_approve_member(member_id: int):
    guard = _require_admin()
    if guard is not None:
        return guard
    with connect() as conn:
        # Get the member's selected locker
        member = conn.execute(
            "SELECT locker_id FROM members WHERE id = ?",
            (member_id,),
        ).fetchone()
        
        if not member:
            return redirect(url_for("admin_pending_approvals"))
        
        locker_id = member["locker_id"]
        
        # Verify the selected locker is still available (not assigned to another member)
        if locker_id:
            locker_check = conn.execute(
                "SELECT status FROM lockers WHERE id = ? AND status = 'available'",
                (locker_id,),
            ).fetchone()
            
            if not locker_check:
                # Selected locker is no longer available, mark approval failed
                detail = f"member_id={member_id}; LOCKER {locker_id} NO LONGER AVAILABLE"
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("admin", str(member_id), "approval_failed", detail),
                )
                return redirect(url_for("admin_pending_approvals"))
        
        expiry_date = (datetime.now() + timedelta(days=30)).isoformat()
        
        # Update member: approve, mark paid, locker_id stays as selected
        conn.execute(
            "UPDATE members SET status = 'approved', payment_status = 'paid', paid_at = datetime('now'), expiry_date = ? WHERE id = ?",
            (expiry_date, member_id),
        )
        
        # Note: Locker status is only for IR sensor state (available/occupied)
        # Assignment is tracked via members.locker_id, not locker status
        if locker_id:
            detail = f"member_id={member_id}; expiry={expiry_date}; locker_id={locker_id}"
        else:
            detail = f"member_id={member_id}; expiry={expiry_date}; NO LOCKER SELECTED"
        
        # Record initial payment
        conn.execute(
            "INSERT INTO payments (member_id, amount, payment_type, notes) VALUES (?, ?, ?, ?)",
            (member_id, 400, "initial", "Member approval and initial payment"),
        )
        conn.execute(
            "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
            ("admin", str(member_id), "member_approved", detail),
        )
        conn.execute(
            "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
            ("admin", str(member_id), "payment_marked_paid", f"member_id={member_id}; via=approve"),
        )
        if locker_id:
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("admin", str(member_id), "locker_assigned", f"locker_id={locker_id}"),
            )
        
        # CRITICAL: Commit all changes
        conn.commit()
    return redirect(url_for("admin.admin_pending"))


@admin_bp.post("/members/<int:member_id>/reject")
def admin_reject_member(member_id: int):
    guard = _require_admin()
    if guard is not None:
        return guard
    with connect() as conn:
        conn.execute("UPDATE members SET status = 'rejected' WHERE id = ?", (member_id,))
        conn.execute(
            "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
            ("admin", str(member_id), "member_rejected", f"member_id={member_id}"),
        )
        conn.commit()
    return redirect(url_for("admin.admin_pending"))


@admin_bp.post("/members/<int:member_id>/mark-paid")
def admin_mark_paid(member_id: int):
    guard = _require_admin()
    if guard is not None:
        return guard
    with connect() as conn:
        conn.execute(
            "UPDATE members SET payment_status = 'paid', paid_at = datetime('now') WHERE id = ?",
            (member_id,),
        )
        # Record payment
        conn.execute(
            "INSERT INTO payments (member_id, amount, payment_type, notes) VALUES (?, ?, ?, ?)",
            (member_id, 400, "manual_payment", "Manually marked as paid"),
        )
        conn.execute(
            "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
            ("admin", str(member_id), "payment_marked_paid", f"member_id={member_id}"),
        )
        conn.commit()
    return redirect(url_for("admin.admin_pending"))


@admin_bp.post("/members/<int:member_id>/renew")
def admin_renew_member(member_id: int):
    guard = _require_admin()
    if guard is not None:
        return guard
    with connect() as conn:
        # Extend membership by 30 days from now
        expiry_date = (datetime.now() + timedelta(days=30)).isoformat()
        conn.execute(
            "UPDATE members SET payment_status = 'paid', paid_at = datetime('now'), expiry_date = ? WHERE id = ?",
            (expiry_date, member_id),
        )
        # Record renewal payment
        conn.execute(
            "INSERT INTO payments (member_id, amount, payment_type, notes) VALUES (?, ?, ?, ?)",
            (member_id, 400, "renewal", f"Membership renewal - expires {expiry_date}"),
        )
        conn.execute(
            "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
            ("admin", str(member_id), "membership_renewed", f"member_id={member_id}; expiry={expiry_date}"),
        )
        conn.commit()
    return redirect(url_for("admin.admin_members"))


@admin_bp.post("/members/<int:member_id>/delete")
def admin_delete_member(member_id: int):
    guard = _require_admin()
    if guard is not None:
        return guard
    with connect() as conn:
        # Get member info before deletion for logging
        member = conn.execute("SELECT full_name, locker_id FROM members WHERE id = ?", (member_id,)).fetchone()
        if member:
            locker_id = member["locker_id"]
            # Free the locker if assigned
            if locker_id:
                conn.execute("UPDATE lockers SET status = 'available' WHERE id = ?", (locker_id,))
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("admin", str(member_id), "locker_freed", f"locker_id={locker_id}"),
                )
            
            # Remove associated payments so revenue totals adjust after deletion
            conn.execute("DELETE FROM payments WHERE member_id = ?", (member_id,))
            
            # Delete the member
            conn.execute("DELETE FROM members WHERE id = ?", (member_id,))
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("admin", str(member_id), "member_deleted", f"member_name={member['full_name']}"),
            )
        
        # CRITICAL: Commit all changes to database
        conn.commit()
    return redirect(url_for("admin.admin_members"))


@admin_bp.get("/payments")
def admin_payments():
    guard = _require_admin()
    if guard is not None:
        return guard
    with connect() as conn:
        payments = conn.execute(
            """
            SELECT p.id, p.member_id, p.amount, p.payment_type, p.payment_date, p.notes,
                   m.full_name
            FROM payments p
            JOIN members m ON p.member_id = m.id
            ORDER BY p.payment_date DESC
            LIMIT 200
            """
        ).fetchall()

        total_revenue = conn.execute(
            "SELECT SUM(amount) AS total FROM payments"
        ).fetchone()["total"] or 0

    return render_template(
        "admin/pages/admin_payments.html",
        payments=payments,
        total_revenue=total_revenue,
        current_page="payments",
        admin_username=session.get("admin_username"),
    )


@admin_bp.get("/access-logs")
def admin_access_logs():
    guard = _require_admin()
    if guard is not None:
        return guard

    # Get query parameters for filtering
    q = (request.args.get("q") or "").strip()
    actor_type = (request.args.get("actor_type") or "").strip()
    action = (request.args.get("action") or "").strip()
    limit = int(request.args.get("limit", 200))

    with connect() as conn:
        # Build query with JOIN to get member/guest names
        query = """
            SELECT 
                al.id, 
                al.actor_type, 
                al.actor_ref, 
                al.action, 
                al.detail, 
                al.created_at,
                COALESCE(m.full_name, 'N/A') as actor_name
            FROM access_logs al
            LEFT JOIN members m ON al.actor_type = 'guest' AND CAST(al.actor_ref AS INTEGER) = m.id
            WHERE al.actor_type NOT IN ('admin', 'system', 'ir_sensor')
        """
        params = []

        if q:
            query += " AND (m.full_name LIKE ? OR al.detail LIKE ? OR al.action LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

        if actor_type:
            query += " AND al.actor_type = ?"
            params.append(actor_type)

        if action:
            query += " AND al.action = ?"
            params.append(action)

        query += " ORDER BY al.id DESC LIMIT ?"
        params.append(limit)

        logs = conn.execute(query, params).fetchall()

        # Get distinct actor types and actions for filter dropdowns (exclude admin/system/ir_sensor)
        actor_types = conn.execute(
            "SELECT DISTINCT actor_type FROM access_logs WHERE actor_type NOT IN ('admin', 'system', 'ir_sensor') ORDER BY actor_type"
        ).fetchall()
        actions = conn.execute(
            "SELECT DISTINCT action FROM access_logs WHERE actor_type NOT IN ('admin', 'system', 'ir_sensor') ORDER BY action"
        ).fetchall()

    return render_template(
        "admin/pages/admin_access_logs.html",
        logs=logs,
        q=q,
        actor_type=actor_type,
        action=action,
        limit=limit,
        actor_types=actor_types,
        actions=actions,
        current_page="access_logs",
        admin_username=session.get("admin_username"),
    )


@admin_bp.get("/ir-logs")
def admin_ir_logs():
    """Display IR sensor occupancy logs."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    limit = int(request.args.get("limit", 100))
    with connect() as conn:
        # Get all IR sensor logs, newest first
        ir_logs = conn.execute(
            """
            SELECT id, actor_type, actor_ref, action, detail, created_at 
            FROM access_logs 
            WHERE actor_type = 'ir_sensor' 
            ORDER BY id DESC 
            LIMIT ?
            """,
            (limit,)
        ).fetchall()
        
        # Count stats
        total_reports = conn.execute(
            "SELECT COUNT(*) AS c FROM access_logs WHERE actor_type = 'ir_sensor'"
        ).fetchone()["c"]
        
        occupied_reports = conn.execute(
            "SELECT COUNT(*) AS c FROM access_logs WHERE actor_type = 'ir_sensor' AND detail LIKE '%status=occupied%'"
        ).fetchone()["c"]
        
        available_reports = conn.execute(
            "SELECT COUNT(*) AS c FROM access_logs WHERE actor_type = 'ir_sensor' AND detail LIKE '%status=available%'"
        ).fetchone()["c"]
    
    stats = {
        "total": total_reports,
        "occupied": occupied_reports,
        "available": available_reports,
    }
    
    return render_template(
        "admin/pages/admin_ir_logs.html",
        ir_logs=ir_logs,
        stats=stats,
        limit=limit,
        current_page="ir_logs",
        admin_username=session.get("admin_username"),
    )


@admin_bp.get("/rfid")
def admin_rfid():
    """Guest RFID card management page."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    q = (request.args.get("q") or "").strip()
    now_local = datetime.now().isoformat()  # Get current local time
    with connect() as conn:
        # Auto-delete expired guests to free their RFID cards for reuse
        expired_guests = conn.execute(
            """
            SELECT id, full_name, rfid_uid, expiry_date, locker_id
            FROM members 
            WHERE member_type = 'guest' AND expiry_date < ?
            """,
            (now_local,)
        ).fetchall()
        
        # Delete expired guests and log each deletion
        for guest in expired_guests:
            # Free the locker if assigned
            if guest["locker_id"]:
                conn.execute("UPDATE lockers SET status = 'available' WHERE id = ?", (guest["locker_id"],))
            
            conn.execute("DELETE FROM members WHERE id = ?", (guest["id"],))
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("system", str(guest["id"]), "guest_auto_deleted", f"rfid={guest['rfid_uid']} freed for reuse (expired at {guest['expiry_date']})"),
            )
        
        if expired_guests:
            conn.commit()
        
        if q:
            guests = conn.execute(
                """
                SELECT id, full_name, rfid_uid, locker_id, expiry_date, created_at
                FROM members
                WHERE member_type = 'guest' AND (full_name LIKE ? OR rfid_uid LIKE ? OR contact_number LIKE ?)
                ORDER BY expiry_date DESC
                LIMIT 200
                """,
                (f"%{q}%", f"%{q}%", f"%{q}%"),
            ).fetchall()
        else:
            guests = conn.execute(
                """
                SELECT id, full_name, rfid_uid, locker_id, expiry_date, created_at
                FROM members
                WHERE member_type = 'guest'
                ORDER BY expiry_date DESC
                LIMIT 200
                """
            ).fetchall()
        
        # Count only active guests (expired ones are now deleted)
        active_count = conn.execute(
            "SELECT COUNT(*) as count FROM members WHERE member_type = 'guest' AND expiry_date >= ?",
            (now_local,)
        ).fetchone()["count"]
        
        expired_count = 0  # No expired guests exist anymore (auto-deleted)
    
    return render_template(
        "admin/pages/admin_rfid.html",
        guests=guests,
        active_count=active_count,
        expired_count=expired_count,
        q=q,
        current_page="rfid",
        admin_username=session.get("admin_username"),
        now=datetime.now(),  # Pass current datetime to template
    )


@admin_bp.get("/lockers")
def admin_lockers():
    """Display all lockers and their assignments."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    now_local = datetime.now().isoformat()  # Get current local time
    with connect() as conn:
        # Auto-delete expired guests to free their RFID cards and lockers for reuse
        expired_guests = conn.execute(
            """
            SELECT id, full_name, rfid_uid, expiry_date, locker_id
            FROM members 
            WHERE member_type = 'guest' AND expiry_date < ?
            """,
            (now_local,)
        ).fetchall()
        
        # Delete expired guests and log each deletion
        for guest in expired_guests:
            # Free the locker if assigned
            if guest["locker_id"]:
                conn.execute("UPDATE lockers SET status = 'available' WHERE id = ?", (guest["locker_id"],))
            
            conn.execute("DELETE FROM members WHERE id = ?", (guest["id"],))
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("system", str(guest["id"]), "guest_auto_deleted", f"rfid={guest['rfid_uid']} freed for reuse (expired at {guest['expiry_date']})"),
            )
        
        if expired_guests:
            conn.commit()
        # Get lockers 1-4 only with their assigned member/guest info
        # Only show APPROVED members (status='approved') or active guests (not expired)
        # This prevents pending members from showing as "assigned" to a locker
        lockers_data = conn.execute(
            """
            SELECT 
                l.id,
                l.label,
                l.status,
                m.id AS member_id,
                m.full_name,
                m.member_type,
                m.status AS member_status,
                m.payment_status,
                m.expiry_date
            FROM lockers l
            LEFT JOIN members m ON l.id = m.locker_id 
                AND (
                    (m.member_type != 'guest' AND m.status = 'approved')  -- Only show APPROVED regular members
                    OR (m.member_type = 'guest' AND m.expiry_date >= ?)  -- Show active guests
                )
            WHERE l.id BETWEEN 1 AND 4
            ORDER BY l.id ASC
            """,
            (now_local,)
        ).fetchall()
        
        # Transform to list of dicts for template
        lockers = []
        for row in lockers_data:
            locker = {
                "id": row["id"],
                "label": row["label"],
                "status": row["status"],
                "assigned_to": None
            }
            if row["member_id"]:
                locker["assigned_to"] = {
                    "id": row["member_id"],
                    "name": row["full_name"],
                    "type": row["member_type"],
                    "member_status": row["member_status"],
                    "payment_status": row["payment_status"],
                    "expiry_date": row["expiry_date"]
                }
            lockers.append(locker)
        
        # Summary stats
        stats = {
            "total": len(lockers),
            "available": sum(1 for l in lockers if l["status"] == "available"),
            "occupied": sum(1 for l in lockers if l["status"] == "occupied"),
        }
    
    return render_template(
        "admin/pages/admin_lockers.html",
        lockers=lockers,
        stats=stats,
        current_page="lockers",
        admin_username=session.get("admin_username"),
        now=datetime.now(),
    )


@admin_bp.get("/available-lockers")
def admin_get_available_lockers():
    """Get list of available lockers for guest assignment (lockers 1-4 only)."""
    guard = _require_admin()
    if guard is not None:
        return {"error": "Unauthorized"}, 401
    
    with connect() as conn:
        lockers = conn.execute(
            "SELECT id, label FROM lockers WHERE id BETWEEN 1 AND 4 ORDER BY id"
        ).fetchall()
    
    return {
        "lockers": [{"id": l["id"], "label": l["label"]} for l in lockers]
    }


@admin_bp.post("/guests/create")
def admin_create_guest():
    """Create a guest with RFID card."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    full_name = (request.form.get("full_name") or "").strip()
    rfid_uid = (request.form.get("rfid_uid") or "").strip()
    locker_id_raw = (request.form.get("locker_id") or "").strip()
    duration_hours = int(request.form.get("duration_hours", 24))
    
    if not full_name or not rfid_uid or not locker_id_raw.isdigit():
        return {"error": "Missing required fields"}, 400
    
    locker_id = int(locker_id_raw)
    
    with connect() as conn:
        # Check for duplicate RFID - but allow if previous guest's access has expired
        existing = conn.execute(
            "SELECT id, member_type, expiry_date FROM members WHERE rfid_uid = ?",
            (rfid_uid,),
        ).fetchone()
        
        if existing and existing["member_type"] == "guest":
            # Check if guest access has expired
            if existing["expiry_date"]:
                expiry = datetime.fromisoformat(existing["expiry_date"])
                if datetime.now() < expiry:
                    # Guest access is still active - cannot reuse
                    return {"error": "RFID card is currently in use"}, 409
            # If expired, delete the old guest to allow reuse
            old_locker_id = conn.execute(
                "SELECT locker_id FROM members WHERE id = ?",
                (existing["id"],),
            ).fetchone()
            if old_locker_id and old_locker_id["locker_id"]:
                conn.execute(
                    "UPDATE lockers SET status = 'available' WHERE id = ?",
                    (old_locker_id["locker_id"],),
                )
            conn.execute("DELETE FROM members WHERE id = ?", (existing["id"],))
        elif existing and existing["member_type"] != "guest":
            # Don't allow reusing member RFIDs
            return {"error": "RFID card already in use by a member"}, 409
        
        # Check locker availability
        locker = conn.execute(
            "SELECT status FROM lockers WHERE id = ?",
            (locker_id,),
        ).fetchone()
        if not locker:
            return {"error": "Locker not found"}, 404
        
        # Create guest (instant access, no payment needed)
        expiry_date = (datetime.now() + timedelta(hours=duration_hours)).isoformat()
        cur = conn.execute(
            """
            INSERT INTO members (full_name, rfid_uid, locker_id, status, payment_status, 
                                expiry_date, member_type, created_at)
            VALUES (?,?,?,'approved','paid',?,'guest',datetime('now'))
            """,
            (full_name, rfid_uid, locker_id, expiry_date),
        )
        guest_id = cur.lastrowid
        
        # Note: Locker status is only for IR sensor state (available/occupied)
        # Assignment is tracked via members.locker_id, not locker status
        
        # Log guest creation
        conn.execute(
            "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
            ("admin", str(guest_id), "guest_created", f"guest_id={guest_id}; rfid={rfid_uid}; expires={expiry_date}"),
        )
        conn.commit()
    
    return {
        "status": "created",
        "guest_id": guest_id,
        "message": f"Guest '{full_name}' created - access expires in {duration_hours} hours"
    }, 201


@admin_bp.post("/guests/<int:guest_id>/delete")
def admin_delete_guest(guest_id: int):
    """Delete a guest and free their locker."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    with connect() as conn:
        guest = conn.execute(
            "SELECT locker_id FROM members WHERE id = ? AND member_type = 'guest'",
            (guest_id,),
        ).fetchone()
        
        if guest and guest["locker_id"]:
            conn.execute(
                "UPDATE lockers SET status = 'available' WHERE id = ?",
                (guest["locker_id"],),
            )
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("admin", str(guest_id), "guest_locker_freed", f"locker_id={guest['locker_id']}"),
            )
        
        conn.execute("DELETE FROM members WHERE id = ?", (guest_id,))
        conn.execute(
            "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
            ("admin", str(guest_id), "guest_deleted", f"guest_id={guest_id}"),
        )
        conn.commit()
    
    return redirect(url_for("admin.admin_rfid"))



def admin_unlock_locker_1():
    device.unlock(1)
    with connect() as conn:
        conn.execute(
            "INSERT INTO access_logs (actor_type, action, detail) VALUES (?,?,?)",
            ("admin", "unlock_requested", "locker_id=1"),
        )
        conn.commit()
    return redirect(url_for("admin.admin_dashboard"))


@admin_bp.post("/locker/1/lock")
def admin_lock_locker_1():
    device.lock(1)
    with connect() as conn:
        conn.execute(
            "INSERT INTO access_logs (actor_type, action, detail) VALUES (?,?,?)",
            ("admin", "lock_requested", "locker_id=1"),
        )
        conn.commit()
    return redirect(url_for("admin.admin_dashboard"))


@admin_bp.get("/settings")
def admin_settings():
    """Display system settings page."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    settings = load_settings()
    
    return render_template(
        "admin/pages/admin_settings.html",
        settings=settings,
        current_page="settings",
        admin_username=session.get("admin_username"),
        now=datetime.now(),
    )


@admin_bp.post("/settings/update")
def admin_settings_update():
    """Update system settings."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    # Collect form data with proper type conversions
    settings_data = {
        "system_name": request.form.get("system_name", "Smart Locker"),
        "timezone": request.form.get("timezone", "Asia/Manila"),
        "currency": request.form.get("currency", "₱"),
        "language": request.form.get("language", "en"),
        "membership_fee": int(request.form.get("membership_fee", 400)),
        "membership_duration": int(request.form.get("membership_duration", 30)),
        "renewal_fee": int(request.form.get("renewal_fee", 400)),
        "renewal_duration": int(request.form.get("renewal_duration", 30)),
        "default_guest_duration": int(request.form.get("default_guest_duration", 24)),
        "max_guest_duration": int(request.form.get("max_guest_duration", 72)),
        "guest_price_per_hour": int(request.form.get("guest_price_per_hour", 50)),
        "facility_name": request.form.get("facility_name", "Smart Locker System"),
        "facility_address": request.form.get("facility_address", "123 Main Street"),
        "facility_phone": request.form.get("facility_phone", "+63 (0) 000-0000"),
        "facility_email": request.form.get("facility_email", ""),
        "unlock_duration": int(request.form.get("unlock_duration", 5)),
        "rfid_timeout": int(request.form.get("rfid_timeout", 10)),
        "fingerprint_timeout": int(request.form.get("fingerprint_timeout", 10)),
        "max_failed_attempts": int(request.form.get("max_failed_attempts", 3)),
        "session_timeout": int(request.form.get("session_timeout", 30)),
        "password_min_length": int(request.form.get("password_min_length", 8)),
        "login_attempts": int(request.form.get("login_attempts", 5)),
        "email_notifications": request.form.get("email_notifications") == "1",
        "sms_notifications": request.form.get("sms_notifications") == "1",
        "low_balance_alert": request.form.get("low_balance_alert") == "1",
        "backup_interval": int(request.form.get("backup_interval", 24)),
        "log_retention": int(request.form.get("log_retention", 90)),
        "restart_time": request.form.get("restart_time", "03:00"),
        "payment_gateway": request.form.get("payment_gateway", "none"),
    }
    
    # Save settings
    if save_settings(settings_data):
        with connect() as conn:
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("admin", session.get("admin_username"), "settings_updated", "System settings updated"),
            )
            conn.commit()
    
    return redirect(url_for("admin.admin_settings"))


@admin_bp.get("/reports")
def admin_reports():
    """Generate reports on system usage, revenue, and access patterns."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    with connect() as conn:
        # Get total stats
        total_members = conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'regular'").fetchone()["c"]
        total_guests = conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'guest'").fetchone()["c"]
        total_access_logs = conn.execute("SELECT COUNT(*) AS c FROM access_logs WHERE actor_type NOT IN ('system', 'ir_sensor')").fetchone()["c"]
        
        # Revenue (₱400 per approved member)
        approved_members = conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'regular' AND status = 'approved' AND payment_status = 'paid'").fetchone()["c"]
        total_revenue = approved_members * 400
        
        # Monthly revenue trend (last 30 days)
        monthly_payments = conn.execute(
            """
            SELECT COUNT(*) AS c, SUM(amount) AS total
            FROM payments
            WHERE datetime(payment_date) >= datetime('now', '-30 days')
            """
        ).fetchone()
        
        # Top access actions (exclude system and IR sensor logs)
        top_actions = conn.execute(
            """
            SELECT action, COUNT(*) AS c
            FROM access_logs
            WHERE actor_type NOT IN ('system', 'ir_sensor')
            GROUP BY action
            ORDER BY c DESC
            LIMIT 10
            """
        ).fetchall()
        
        # Access by actor type (last 7 days)
        access_by_type = conn.execute(
            """
            SELECT actor_type, COUNT(*) AS c
            FROM access_logs
            WHERE datetime(created_at) >= datetime('now', '-7 days')
            GROUP BY actor_type
            ORDER BY c DESC
            """
        ).fetchall()
    
    return render_template(
        "admin/pages/admin_reports.html",
        current_page="reports",
        admin_username=session.get("admin_username"),
        stats={
            "total_members": total_members,
            "total_guests": total_guests,
            "total_access_logs": total_access_logs,
            "approved_members": approved_members,
            "total_revenue": total_revenue,
            "monthly_revenue": monthly_payments["total"] or 0,
            "monthly_access_count": monthly_payments["c"] or 0,
        },
        top_actions=top_actions,
        access_by_type=access_by_type,
    )


@admin_bp.get("/reports/export-csv")
def export_reports_csv():
    """Export reports data as CSV."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    import csv
    from io import StringIO
    from flask import make_response
    from datetime import datetime
    
    with connect() as conn:
        # Get stats
        total_members = conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'regular'").fetchone()["c"]
        total_guests = conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'guest'").fetchone()["c"]
        total_access_logs = conn.execute("SELECT COUNT(*) AS c FROM access_logs WHERE actor_type NOT IN ('system', 'ir_sensor')").fetchone()["c"]
        approved_members = conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'regular' AND status = 'approved' AND payment_status = 'paid'").fetchone()["c"]
        total_revenue = approved_members * 400
        
        monthly_payments = conn.execute(
            """
            SELECT COUNT(*) AS c, SUM(amount) AS total
            FROM payments
            WHERE datetime(payment_date) >= datetime('now', '-30 days')
            """
        ).fetchone()
        
        top_actions = conn.execute(
            """
            SELECT action, COUNT(*) AS c
            FROM access_logs
            WHERE actor_type NOT IN ('system', 'ir_sensor')
            GROUP BY action
            ORDER BY c DESC
            LIMIT 10
            """
        ).fetchall()
        
        access_by_type = conn.execute(
            """
            SELECT actor_type, COUNT(*) AS c
            FROM access_logs
            WHERE datetime(created_at) >= datetime('now', '-7 days')
            GROUP BY actor_type
            ORDER BY c DESC
            """
        ).fetchall()
    
    output = StringIO()
    writer = csv.writer(output)
    
    # Write summary stats
    writer.writerow(["REPORTS & ANALYTICS", datetime.now().strftime("%Y-%m-%d %H:%M")])
    writer.writerow([])
    writer.writerow(["SUMMARY STATISTICS"])
    writer.writerow(["Metric", "Value"])
    writer.writerow(["Total Members", total_members])
    writer.writerow(["Total Guests", total_guests])
    writer.writerow(["Approved Members", approved_members])
    writer.writerow(["Total Revenue", f"₱{total_revenue:,}"])
    writer.writerow(["Monthly Revenue (Last 30 Days)", f"₱{monthly_payments['total'] or 0:,}"])
    writer.writerow(["Total Access Events", total_access_logs])
    
    # Top Actions
    writer.writerow([])
    writer.writerow(["TOP ACTIONS (ALL TIME)"])
    writer.writerow(["Action", "Count"])
    for action in top_actions:
        writer.writerow([action['action'].replace('_', ' ').title(), action['c']])
    
    # Access by Type
    writer.writerow([])
    writer.writerow(["ACCESS BY TYPE (LAST 7 DAYS)"])
    writer.writerow(["Type", "Count"])
    for entry in access_by_type:
        writer.writerow([entry['actor_type'].title(), entry['c']])
    
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename=locker-reports-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    return response


@admin_bp.get("/reports/export-pdf")
def export_reports_pdf():
    """Export reports data as PDF."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    try:
        from reportlab.lib.pagesizes import letter, A4
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from io import BytesIO
        from flask import make_response
        from datetime import datetime
    except ImportError:
        return {"error": "reportlab not installed. Run: pip install reportlab"}, 500
    
    with connect() as conn:
        # Get stats
        total_members = conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'regular'").fetchone()["c"]
        total_guests = conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'guest'").fetchone()["c"]
        total_access_logs = conn.execute("SELECT COUNT(*) AS c FROM access_logs WHERE actor_type NOT IN ('system', 'ir_sensor')").fetchone()["c"]
        approved_members = conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'regular' AND status = 'approved' AND payment_status = 'paid'").fetchone()["c"]
        total_revenue = approved_members * 400
        
        monthly_payments = conn.execute(
            """
            SELECT COUNT(*) AS c, SUM(amount) AS total
            FROM payments
            WHERE datetime(payment_date) >= datetime('now', '-30 days')
            """
        ).fetchone()
        
        top_actions = conn.execute(
            """
            SELECT action, COUNT(*) AS c
            FROM access_logs
            WHERE actor_type NOT IN ('system', 'ir_sensor')
            GROUP BY action
            ORDER BY c DESC
            LIMIT 10
            """
        ).fetchall()
        
        access_by_type = conn.execute(
            """
            SELECT actor_type, COUNT(*) AS c
            FROM access_logs
            WHERE datetime(created_at) >= datetime('now', '-7 days')
            GROUP BY actor_type
            ORDER BY c DESC
            """
        ).fetchall()
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    elements = []
    styles = getSampleStyleSheet()
    
    # Custom style for title
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=16,
        textColor=colors.HexColor('#0066cc'),
        spaceAfter=6,
        alignment=1
    )
    
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        fontSize=12,
        textColor=colors.HexColor('#0066cc'),
        spaceAfter=6,
        spaceBefore=6
    )
    
    # Title
    elements.append(Paragraph("Locker System - Reports & Analytics", title_style))
    elements.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
    elements.append(Spacer(1, 0.3*inch))
    
    # Summary Statistics Table
    elements.append(Paragraph("Summary Statistics", heading_style))
    summary_data = [
        ["Metric", "Value"],
        ["Total Members", str(total_members)],
        ["Total Guests", str(total_guests)],
        ["Approved Members", str(approved_members)],
        ["Total Revenue", f"₱{total_revenue:,}"],
        ["Monthly Revenue (Last 30 Days)", f"₱{monthly_payments['total'] or 0:,}"],
        ["Total Access Events", str(total_access_logs)],
    ]
    
    summary_table = Table(summary_data, colWidths=[3*inch, 2*inch])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0066cc')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 0.2*inch))
    
    # Top Actions Table
    elements.append(Paragraph("Top Actions (All Time)", heading_style))
    actions_data = [["Action", "Count"]]
    for action in top_actions:
        actions_data.append([action['action'].replace('_', ' ').title(), str(action['c'])])
    
    actions_table = Table(actions_data, colWidths=[4*inch, 1*inch])
    actions_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0066cc')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    elements.append(actions_table)
    elements.append(Spacer(1, 0.2*inch))
    
    # Access by Type Table
    elements.append(Paragraph("Access by Type (Last 7 Days)", heading_style))
    type_data = [["Type", "Count"]]
    for entry in access_by_type:
        type_data.append([entry['actor_type'].title(), str(entry['c'])])
    
    type_table = Table(type_data, colWidths=[4*inch, 1*inch])
    type_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0066cc')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    elements.append(type_table)
    
    # Build PDF
    doc.build(elements)
    buffer.seek(0)
    
    response = make_response(buffer.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename=locker-reports-{datetime.now().strftime('%Y%m%d-%H%M%S')}.pdf"
    response.headers["Content-Type"] = "application/pdf"
    return response


# ========== Access Logs Export ==========
@admin_bp.get("/access-logs/export-csv")
def export_access_logs_csv():
    """Export access logs as CSV."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    import csv
    from io import StringIO
    from flask import make_response
    
    with connect() as conn:
        logs = conn.execute(
            """
            SELECT 
                al.id, 
                al.created_at,
                al.actor_type,
                COALESCE(m.full_name, 'N/A') as actor_name,
                al.action, 
                al.detail
            FROM access_logs al
            LEFT JOIN members m ON al.actor_type = 'guest' AND CAST(al.actor_ref AS INTEGER) = m.id
            WHERE al.actor_type NOT IN ('admin', 'system', 'ir_sensor')
            ORDER BY al.id DESC
            LIMIT 5000
            """
        ).fetchall()
    
    output = StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow(["ID", "Timestamp", "Actor Type", "Actor Name", "Action", "Details"])
    
    # Write data
    for log in logs:
        writer.writerow([
            log['id'],
            log['created_at'],
            log['actor_type'].title(),
            log['actor_name'],
            log['action'].replace('_', ' ').title(),
            log['detail'] or ''
        ])
    
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename=access-logs-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    return response


@admin_bp.get("/access-logs/export-pdf")
def export_access_logs_pdf():
    """Export access logs as PDF."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    from reportlab.lib.pagesizes import letter, A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from io import BytesIO
    from flask import make_response
    
    with connect() as conn:
        logs = conn.execute(
            """
            SELECT 
                al.id, 
                al.created_at,
                al.actor_type,
                COALESCE(m.full_name, 'N/A') as actor_name,
                al.action, 
                al.detail
            FROM access_logs al
            LEFT JOIN members m ON al.actor_type = 'guest' AND CAST(al.actor_ref AS INTEGER) = m.id
            WHERE al.actor_type NOT IN ('admin', 'system', 'ir_sensor')
            ORDER BY al.id DESC
            LIMIT 5000
            """
        ).fetchall()
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    elements = []
    
    # Title
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading2'],
        fontSize=14,
        textColor=colors.HexColor('#1e40af'),
        spaceAfter=6,
        fontName='Helvetica-Bold'
    )
    elements.append(Paragraph("Access Logs Report", title_style))
    elements.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
    elements.append(Spacer(1, 0.3*inch))
    
    # Prepare data for table
    data = [["ID", "Timestamp", "Actor Type", "Actor Name", "Action", "Details"]]
    for log in logs:
        data.append([
            str(log['id']),
            log['created_at'][:16] if log['created_at'] else '',
            log['actor_type'].title(),
            log['actor_name'],
            log['action'].replace('_', ' ').title(),
            log['detail'][:50] + "..." if log['detail'] and len(log['detail']) > 50 else (log['detail'] or '')
        ])
    
    # Build table with styling
    access_table = Table(data, colWidths=[0.6*inch, 1.2*inch, 0.8*inch, 1.2*inch, 1*inch, 2*inch])
    access_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e40af')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('BACKGROUND', (0, 1), (-1, -1), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0f0f0')])
    ]))
    elements.append(access_table)
    
    # Build PDF
    doc.build(elements)
    buffer.seek(0)
    
    response = make_response(buffer.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename=access-logs-{datetime.now().strftime('%Y%m%d-%H%M%S')}.pdf"
    response.headers["Content-Type"] = "application/pdf"
    return response


@admin_bp.get("/admin-activity")
def admin_activity():
    """View admin actions and system activity."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    q = (request.args.get("q") or "").strip()
    actor_type = (request.args.get("actor_type") or "").strip()
    limit = int(request.args.get("limit", 500))
    
    with connect() as conn:
        # Build query - ONLY show admin/system actions, EXCLUDE device/guest/member access
        query = """
            SELECT id, actor_type, actor_ref, action, detail, created_at
            FROM access_logs
            WHERE actor_type IN ('admin', 'system')
        """
        params = []
        
        if q:
            query += " AND (actor_ref LIKE ? OR detail LIKE ? OR action LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
        
        if actor_type:
            query += " AND actor_type = ?"
            params.append(actor_type)
        
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        
        logs = conn.execute(query, params).fetchall()
        
        # Get actor type stats (only admin/system)
        actor_stats = conn.execute(
            """
            SELECT actor_type, COUNT(*) AS c
            FROM access_logs
            WHERE actor_type IN ('admin', 'system')
            GROUP BY actor_type
            ORDER BY c DESC
            """
        ).fetchall()
    
    return render_template(
        "admin/pages/admin_activity.html",
        current_page="admin_activity",
        admin_username=session.get("admin_username"),
        logs=logs,
        actor_stats=actor_stats,
        q=q,
        actor_type=actor_type,
        limit=limit,
    )
