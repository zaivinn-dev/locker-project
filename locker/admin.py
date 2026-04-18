"""Admin routes for locker management system."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import flask
import hmac
import requests
from flask import Blueprint, jsonify, make_response, redirect, render_template, request, session, url_for
import json
import os

try:
    from .db import connect
    from .device import get_device_controller
except ImportError:
    from db import connect
    from device import get_device_controller

# Shared enrollment state (same as in web.py)
fingerprint_enrollment_state = {
    "pending": False,
    "enrolled_uid": None,
}

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def json_error(error: str, message: str, status_code: int = 400):
    return make_response(jsonify({"error": error, "message": message}), status_code)


def json_success(payload: dict, status_code: int = 200):
    return make_response(jsonify(payload), status_code)

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

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
MASTER_UNLOCK_CONFIRM_TEXT = os.getenv("MASTER_UNLOCK_CONFIRM_TEXT", "UNLOCK ALL")

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


def _load_locker_statuses():
    """Load locker assignments and current physical lock state."""
    lockers = []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT l.id, l.label, l.status, m.id AS member_id, m.full_name, m.member_type
            FROM lockers l
            LEFT JOIN members m ON l.id = m.locker_id
            ORDER BY l.id ASC
            """
        ).fetchall()

    for row in rows:
        locker = {
            "id": row["id"],
            "label": row["label"],
            "db_status": row["status"],
            "assigned_to": None,
            "locked": None,
            "item_detected": None,
            "device_error": None,
        }
        if row["member_id"]:
            locker["assigned_to"] = {
                "id": row["member_id"],
                "name": row["full_name"],
                "member_type": row["member_type"],
            }
        try:
            state = device.get_locker(row["id"])
            locker["locked"] = state.locked
            locker["item_detected"] = state.item_detected
        except requests.exceptions.RequestException as exc:
            locker["device_error"] = str(exc)
        except Exception as exc:
            locker["device_error"] = str(exc)
        lockers.append(locker)

    return lockers


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


def verify_admin_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username, ADMIN_USERNAME) and hmac.compare_digest(password, ADMIN_PASSWORD)


@admin_bp.post("/login")
def admin_login_submit():
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()

    if verify_admin_credentials(username, password):
        session["admin_logged_in"] = True
        session["admin_username"] = username
        session["admin_id"] = 1
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


@admin_bp.post("/unlock-all")
def admin_unlock_all():
    guard = _require_admin()
    if guard is not None:
        flask.flash("Action denied: Not logged in as admin", "danger")
        return guard

    confirm_text = (request.form.get("confirm_text") or "").strip()
    admin_password = (request.form.get("admin_password") or "").strip()

    if confirm_text != MASTER_UNLOCK_CONFIRM_TEXT:
        flask.flash(f"Unlock cancelled: type '{MASTER_UNLOCK_CONFIRM_TEXT}' to confirm.", "danger")
        return redirect(url_for("admin.admin_dashboard"))

    if not verify_admin_credentials(session.get("admin_username", ""), admin_password):
        flask.flash("Unlock cancelled: invalid admin password.", "danger")
        return redirect(url_for("admin.admin_dashboard"))

    device_controller = get_device_controller()
    locker_ids = [1, 2, 3, 4]
    results = []
    errors = []

    for locker_id in locker_ids:
        try:
            device_controller.unlock(int(locker_id))
            results.append(locker_id)
        except requests.exceptions.RequestException as exc:
            errors.append(f"Locker {locker_id}: {str(exc)}")
        except Exception as exc:
            errors.append(f"Locker {locker_id}: {str(exc)}")

    with connect() as conn:
        detail = f"locker_ids={','.join(str(i) for i in locker_ids)}; result={ 'success' if not errors else 'partial_failure' }; details={';'.join(errors) if errors else 'all_unlocked'}"
        conn.execute(
            "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
            ("admin", session.get("admin_username", "unknown"), "master_unlock_all", detail),
        )
        conn.commit()

    if errors:
        flask.flash(f"Master unlock completed with errors: {'; '.join(errors)}", "warning")
    else:
        flask.flash("All lockers have been unlocked successfully.", "success")

    return redirect(url_for("admin.admin_dashboard"))


@admin_bp.get("/reset-data")
def reset_data_get():
    return redirect(url_for("admin.admin_settings"))


@admin_bp.post("/reset-data")
def reset_data():
    debug_info = ["Reset attempt started"]
    
    guard = _require_admin()
    if guard is not None:
        debug_info.append("Admin authentication failed - redirecting to login")
        flask.flash("Reset failed: Not logged in as admin", "danger")
        return guard
    
    debug_info.append("Admin authentication passed")
    
    # Require confirmation
    confirm = request.form.get("confirm", "").strip()
    debug_info.append(f"Confirmation text received: '{confirm}'")
    
    if confirm != "RESET_ALL_DATA":
        debug_info.append("Confirmation text incorrect")
        flask.flash(f"Reset cancelled - confirmation text incorrect. Received: '{confirm}'", "danger")
        return redirect(url_for("admin.admin_settings"))
    
    debug_info.append("Confirmation text correct, proceeding with reset")
    
    try:
        with connect() as conn:
            debug_info.append("Connected to database")
            
            # Log the reset action
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("admin", session.get("admin_username", "unknown"), "system_reset", "All data reset initiated"),
            )
            debug_info.append("Logged reset action")
            
            # Delete all payments first (due to foreign key constraints)
            result = conn.execute("DELETE FROM payments")
            debug_info.append(f"Deleted {result.rowcount} payments")
            
            # Delete all members (regular and guest)
            result = conn.execute("DELETE FROM members")
            debug_info.append(f"Deleted {result.rowcount} members")
            
            # Delete all access logs
            result = conn.execute("DELETE FROM access_logs")
            debug_info.append(f"Deleted {result.rowcount} access logs")
            
            # Reset all lockers to available
            result = conn.execute("UPDATE lockers SET status = 'available'")
            debug_info.append(f"Reset {result.rowcount} lockers")
            
            # Reset fingerprint enrollment state
            global fingerprint_enrollment_state
            fingerprint_enrollment_state = {
                "pending": False,
                "enrolled_uid": None,
            }
            debug_info.append("Reset fingerprint enrollment state")
            
            # Clear all fingerprint templates from device
            device = get_device_controller()
            if device.clear_fingerprint_templates():
                debug_info.append("Cleared all fingerprint templates from device")
            else:
                debug_info.append("Warning: Failed to clear fingerprint templates from device")
            
            conn.commit()
            debug_info.append("Transaction committed successfully")
        
        debug_info.append("Reset completed successfully")
        flask.flash("All data has been reset successfully", "success")
        return redirect(url_for("admin.admin_dashboard"))
        
    except Exception as e:
        # Log the error and show message
        error_msg = f"Reset failed: {str(e)}"
        debug_info.append(f"ERROR: {error_msg}")
        flask.flash(f"{error_msg}\nDebug: {' | '.join(debug_info)}", "danger")
        return redirect(url_for("admin.admin_settings"))


@admin_bp.post("/clear-fingerprints")
def clear_fingerprints():
    """Clear all fingerprint templates from device."""
    guard = _require_admin()
    if guard is not None:
        flask.flash("Access denied: Not logged in as admin", "danger")
        return guard
    
    try:
        device = get_device_controller()
        if device.clear_fingerprint_templates():
            # Log the action
            with connect() as conn:
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("admin", session.get("admin_username", "unknown"), "fingerprint_clear", "All fingerprint templates cleared from device"),
                )
                conn.commit()
            
            flask.flash("All fingerprint templates have been cleared from the device", "success")
        else:
            # For devices that don't support clearing (like ESP32), we can still clear the database associations
            # and reset the enrollment state
            global fingerprint_enrollment_state
            fingerprint_enrollment_state = {
                "pending": False,
                "enrolled_uid": None,
            }
            
            # Log the action
            with connect() as conn:
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("admin", session.get("admin_username", "unknown"), "fingerprint_clear", "Fingerprint enrollment state reset (device clearing not supported)"),
                )
                conn.commit()
            
            flask.flash("Fingerprint enrollment state has been reset. Device fingerprint clearing requires firmware update.", "warning")
    except Exception as e:
        flask.flash(f"Error clearing fingerprints: {str(e)}", "danger")
    
    return redirect(url_for("admin.admin_settings"))


@admin_bp.get("")
def admin_dashboard():
    guard = _require_admin()
    if guard is not None:
        return guard
    with connect() as conn:
        # NOTE: Expired guest records are now retained so expired RFID cards remain visible
        # in the admin audit pages and can still be marked as returned or lost.
        totals = {
            "members_total": conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'regular'").fetchone()["c"],
            "members_pending": conn.execute(
                "SELECT COUNT(*) AS c FROM members WHERE member_type = 'regular' AND (status = 'pending' OR payment_status != 'paid')"
            ).fetchone()["c"],
            "members_confirmed": conn.execute(
                "SELECT COUNT(*) AS c FROM members WHERE member_type = 'regular' AND status = 'approved' AND payment_status = 'paid'"
            ).fetchone()["c"],
            "guests_total": conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'guest'").fetchone()["c"],
            "guests_active": conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'guest'").fetchone()["c"],
            "lockers_total": conn.execute("SELECT COUNT(*) AS c FROM lockers").fetchone()["c"],
            "lockers_available": conn.execute("SELECT COUNT(*) AS c FROM lockers WHERE status = 'available'").fetchone()["c"],
            "lockers_occupied": conn.execute("SELECT COUNT(*) AS c FROM lockers WHERE status = 'occupied'").fetchone()["c"],
            "logs_total": conn.execute("SELECT COUNT(*) AS c FROM access_logs").fetchone()["c"],
            "revenue_total": conn.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM payments").fetchone()["total"],
        }

        monthly_revenue = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE datetime(payment_date) >= datetime('now', '-29 days')"
        ).fetchone()["total"]

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

    locker_1 = None
    try:
        locker_1 = device.get_locker(1)
    except requests.exceptions.RequestException as exc:
        flask.current_app.logger.warning(
            "ESP32 timeout or connection error while fetching locker status: %s", exc
        )

    return render_template(
        "admin/pages/admin_dashboard.html",
        members=members,
        logs=logs,
        locker_1=locker_1,
        current_page="dashboard",
        admin_username=session.get("admin_username"),
        totals={**totals, "monthly_revenue": monthly_revenue},
        member_status_counts=member_status_counts,
        activity_7d=activity_7d,
        master_unlock_text=MASTER_UNLOCK_CONFIRM_TEXT,
    )


@admin_bp.get("/members")
def admin_members():
    guard = _require_admin()
    if guard is not None:
        return guard
    q = (request.args.get("q") or "").strip()
    with connect() as conn:
        # NOTE: Expired guest records are now retained so expired RFID cards remain visible
        # in the admin audit pages and can still be marked as returned or lost.
        pass
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
        
        expiry_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        
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
        expiry_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
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
        # Get member info before deletion for logging and cleanup
        member = conn.execute(
            "SELECT full_name, locker_id, fingerprint_uid, rfid_uid FROM members WHERE id = ?",
            (member_id,),
        ).fetchone()
        if member:
            locker_id = member["locker_id"]
            fingerprint_uid = member["fingerprint_uid"]
            rfid_uid = member["rfid_uid"]

            # Free the locker if assigned
            if locker_id:
                conn.execute("UPDATE lockers SET status = 'available' WHERE id = ?", (locker_id,))
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("admin", str(member_id), "locker_freed", f"locker_id={locker_id}"),
                )

            # Remove associated payments so revenue totals adjust after deletion
            conn.execute("DELETE FROM payments WHERE member_id = ?", (member_id,))

            # Attempt fingerprint cleanup on attached device when this member had a fingerprint registered
            if fingerprint_uid:
                device = get_device_controller()
                if device.clear_fingerprint_templates():
                    conn.execute(
                        "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                        ("admin", str(member_id), "fingerprint_cleared", f"fingerprint_uid={fingerprint_uid}"),
                    )
                else:
                    conn.execute(
                        "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                        ("admin", str(member_id), "fingerprint_cleanup_failed", f"fingerprint_uid={fingerprint_uid} (device cleanup unsupported)"),
                    )

            # Delete the member
            conn.execute("DELETE FROM members WHERE id = ?", (member_id,))
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                (
                    "admin",
                    str(member_id),
                    "member_deleted",
                    f"member_name={member['full_name']} fingerprint_uid={fingerprint_uid or 'none'} rfid_uid={rfid_uid or 'none'}",
                ),
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


@admin_bp.get("/rfid")
def admin_rfid():
    """Guest RFID card lifecycle management and guest list report page."""
    guard = _require_admin()
    if guard is not None:
        return guard

    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "all")  # all|active|inactive
    sort_by = request.args.get("sort", "created_at")  # created_at|total_payments|name
    sort_order = request.args.get("order", "desc")  # asc|desc

    with connect() as conn:
        base_query = """
        SELECT
            m.id,
            m.full_name,
            m.rfid_uid,
            m.locker_id,
            CASE
                WHEN LOWER(TRIM(m.category)) = 'student' THEN 'student'
                ELSE 'regular'
            END AS category,
            m.created_at,
            COALESCE((SELECT SUM(amount) FROM payments p2 WHERE p2.member_id = m.id), 0) AS total_payments,
            COALESCE((SELECT COUNT(*) FROM payments p2 WHERE p2.member_id = m.id), 0) AS payment_count,
            COUNT(al.id) AS total_access_events,
            MAX(al.created_at) AS last_access_time,
            (SELECT grc.status
               FROM guest_rfid_cards grc
               WHERE grc.guest_id = m.id
               ORDER BY grc.issue_time DESC
               LIMIT 1) AS card_status
        FROM members m
        LEFT JOIN access_logs al ON al.actor_type = 'guest' AND al.actor_ref = m.rfid_uid
        WHERE m.member_type = 'guest'
        """

        params = []

        if q:
            base_query += " AND (m.full_name LIKE ? OR m.rfid_uid LIKE ? OR CAST(m.id AS TEXT) LIKE ? OR m.category LIKE ? )"
            search_param = f"%{q}%"
            params.extend([search_param, search_param, search_param, search_param])

        if status_filter == "active":
            base_query += " AND EXISTS ("
            base_query += "SELECT 1 FROM guest_rfid_cards grc "
            base_query += "WHERE grc.guest_id = m.id "
            base_query += "  AND grc.issue_time = (SELECT MAX(issue_time) FROM guest_rfid_cards grc2 WHERE grc2.guest_id = m.id) "
            base_query += "  AND grc.status = 'ACTIVE')"
        elif status_filter == "inactive":
            base_query += " AND EXISTS ("
            base_query += "SELECT 1 FROM guest_rfid_cards grc "
            base_query += "WHERE grc.guest_id = m.id "
            base_query += "  AND grc.issue_time = (SELECT MAX(issue_time) FROM guest_rfid_cards grc2 WHERE grc2.guest_id = m.id) "
            base_query += "  AND grc.status != 'ACTIVE')"

        base_query += " GROUP BY m.id, m.full_name, m.rfid_uid, m.locker_id, m.category, m.created_at"

        sort_column_map = {
            "created_at": "m.created_at",
            "total_payments": "total_payments",
            "name": "m.full_name"
        }
        sort_column = sort_column_map.get(sort_by, "m.created_at")
        base_query += f" ORDER BY {sort_column} {'DESC' if sort_order == 'desc' else 'ASC'}"

        guests = conn.execute(base_query, params).fetchall()

        total_guests = conn.execute(
            "SELECT COUNT(*) AS c FROM members WHERE member_type = 'guest'"
        ).fetchone()["c"]

        active_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM members m
            WHERE m.member_type = 'guest'
              AND EXISTS (
                  SELECT 1 FROM guest_rfid_cards grc
                  WHERE grc.guest_id = m.id
                    AND grc.issue_time = (
                        SELECT MAX(issue_time) FROM guest_rfid_cards grc2 WHERE grc2.guest_id = m.id
                    )
                    AND grc.status = 'ACTIVE'
              )
            """
        ).fetchone()["c"]

        inactive_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM members m
            WHERE m.member_type = 'guest'
              AND EXISTS (
                  SELECT 1 FROM guest_rfid_cards grc
                  WHERE grc.guest_id = m.id
                    AND grc.issue_time = (
                        SELECT MAX(issue_time) FROM guest_rfid_cards grc2 WHERE grc2.guest_id = m.id
                    )
                    AND grc.status != 'ACTIVE'
              )
            """
        ).fetchone()["c"]

        total_revenue = conn.execute(
            """
            SELECT COALESCE(SUM(p.amount), 0) AS total
            FROM payments p
            JOIN members m ON p.member_id = m.id
            WHERE m.member_type = 'guest'
            """
        ).fetchone()["total"]

        total_access_events = conn.execute(
            "SELECT COUNT(*) AS c FROM access_logs WHERE actor_type = 'guest'"
        ).fetchone()["c"]

    guests = [dict(g) for g in guests]

    return render_template(
        "admin/pages/admin_rfid.html",
        current_page="rfid",
        admin_username=session.get("admin_username"),
        current_admin_id=session.get("admin_id", 1),
        guests=guests,
        q=q,
        status_filter=status_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        active_count=active_count,
        inactive_count=inactive_count,
        total_revenue=total_revenue,
        total_access_events=total_access_events,
        now=datetime.now(),
    )


@admin_bp.get("/lockers")
def admin_lockers():
    """Display all lockers and their assignments."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        # NOTE: Guest locker assignments are retained even when guest access no longer expires.
        # Only release a locker when the guest no longer has an active card record.
        conn.execute(
            """
            UPDATE members
               SET locker_id = NULL
             WHERE member_type = 'guest'
               AND locker_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM guest_rfid_cards grc
                    WHERE grc.guest_id = members.id
                      AND grc.status = 'ACTIVE'
               )
            """,
        )
        conn.execute(
            """
            UPDATE lockers
               SET status = 'available'
             WHERE status = 'occupied'
               AND id NOT IN (SELECT locker_id FROM members WHERE locker_id IS NOT NULL)
            """,
        )

    with connect() as conn:
        # Get all lockers with their assigned member/guest info
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
                    (m.member_type != 'guest' AND m.status = 'approved')
                    OR (m.member_type = 'guest')
                )
            ORDER BY l.id ASC
            """
        ).fetchall()
        
        # Transform to list of dicts for template
        lockers = []
        for row in lockers_data:
            locker_type = 'member' if row["id"] in (1, 2) else 'guest'
            locker = {
                "id": row["id"],
                "label": row["label"],
                "status": row["status"],
                "type": locker_type,
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
            "available": sum(
                1 for l in lockers
                if (l["type"] == "member" and l["assigned_to"] is None)
                or (l["type"] == "guest" and l["assigned_to"] is None and l["status"] == "available")
            ),
            "occupied": sum(
                1 for l in lockers
                if l["type"] == "guest" and l["status"] == "occupied"
            ),
            "assigned": sum(1 for l in lockers if l["assigned_to"] is not None)
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
    """Get list of available lockers for guest assignment."""
    guard = _require_admin()
    if guard is not None:
        return {"error": "Unauthorized"}, 401

    now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        conn.execute(
            """
            UPDATE lockers
               SET status = 'available'
             WHERE status = 'occupied'
               AND id NOT IN (SELECT locker_id FROM members WHERE locker_id IS NOT NULL)
            """
        )

        lockers = conn.execute(
            """
            SELECT l.id, l.label
            FROM lockers l
            LEFT JOIN members m ON l.id = m.locker_id AND m.member_type = 'guest'
            WHERE m.id IS NULL
              AND l.id IN (3, 4)
            ORDER BY l.id
            """
        ).fetchall()

    return {"lockers": [{"id": l["id"], "label": l["label"]} for l in lockers]}


@admin_bp.get("/guest-details/<int:guest_id>")
def admin_guest_details(guest_id):
    """Get detailed information about a specific guest."""
    guard = _require_admin()
    if guard is not None:
        return {"error": "Unauthorized"}, 401

    with connect() as conn:
        # Get guest basic info
        guest = conn.execute(
            "SELECT * FROM members WHERE id = ? AND member_type = 'guest'",
            (guest_id,)
        ).fetchone()

        if not guest:
            return {"error": "Guest not found"}, 404

        guest_data = dict(guest)

        # Get payment history
        payments = conn.execute(
            "SELECT * FROM payments WHERE member_id = ? ORDER BY payment_date DESC",
            (guest_id,)
        ).fetchall()

        # Get access log summary
        access_summary = conn.execute(
            """
            SELECT
                COUNT(*) as total_events,
                MIN(created_at) as first_access,
                MAX(created_at) as last_access,
                action,
                COUNT(*) as action_count
            FROM access_logs
            WHERE actor_type = 'guest' AND actor_ref = ?
            GROUP BY action
            ORDER BY action_count DESC
            """,
            (guest['rfid_uid'],)
        ).fetchall() if guest['rfid_uid'] else []

        # Get recent access logs
        recent_access = conn.execute(
            "SELECT * FROM access_logs WHERE actor_type = 'guest' AND actor_ref = ? ORDER BY created_at DESC LIMIT 10",
            (guest['rfid_uid'],)
        ).fetchall() if guest['rfid_uid'] else []

        guest_data.update({
            'payments': [dict(p) for p in payments],
            'access_summary': [dict(a) for a in access_summary],
            'recent_access': [dict(a) for a in recent_access],
            'total_payments': sum(p['amount'] for p in payments),
            'payment_count': len(payments),
            'total_access_events': sum(a['total_events'] for a in access_summary) if access_summary else 0
        })

    return guest_data


@admin_bp.post("/extend-guest-access")
def admin_extend_guest_access():
    """Extend a guest's access time."""
    guard = _require_admin()
    if guard is not None:
        return {"error": "Unauthorized"}, 401

    guest_id = request.form.get("guest_id")
    additional_hours = 24

    if not guest_id:
        return {"error": "Guest ID required"}, 400

    with connect() as conn:
        # Get current guest info
        guest = conn.execute(
            "SELECT * FROM members WHERE id = ? AND member_type = 'guest'",
            (guest_id,)
        ).fetchone()

        if not guest:
            return {"error": "Guest not found"}, 404

        # Calculate new expiry time. If the guest has already expired, extend from now.
        current_expiry = datetime.fromisoformat(guest['expiry_date']) if guest['expiry_date'] else datetime.now()
        now = datetime.now()
        if current_expiry < now:
            current_expiry = now
        new_expiry = current_expiry + timedelta(hours=additional_hours)

        # Update guest member expiry
        conn.execute(
            "UPDATE members SET expiry_date = ? WHERE id = ?",
            (new_expiry.strftime("%Y-%m-%d %H:%M:%S"), guest_id)
        )

        # Reactivate any expired or active guest RFID card records for this guest.
        conn.execute(
            "UPDATE guest_rfid_cards SET status = 'ACTIVE', expires_at = ? WHERE guest_id = ? AND status IN ('ACTIVE', 'EXPIRED')",
            (new_expiry.strftime("%Y-%m-%d %H:%M:%S"), guest_id)
        )

        # Log the extension
        conn.execute(
            "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
            ("admin", str(guest_id), "access_extended", f"Extended by 24 hours until {new_expiry.strftime('%Y-%m-%d %H:%M:%S')}"),
        )

    return {"success": True, "message": "Access extended by 24 hours"}


@admin_bp.get("/export-guests")
def admin_export_guests():
    """Export guest list to CSV."""
    guard = _require_admin()
    if guard is not None:
        return {"error": "Unauthorized"}, 401

    import csv
    from io import StringIO

    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "all")

    now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with connect() as conn:
        # Same query as guest list
        base_query = """
        SELECT
            m.id,
            m.full_name,
            m.contact_number,
            m.address,
            m.rfid_uid,
            m.locker_id,
            m.created_at,
            COALESCE(SUM(p.amount), 0) as total_payments,
            COUNT(p.id) as payment_count,
            COUNT(al.id) as total_access_events,
            MAX(al.created_at) as last_access_time,
            (SELECT grc.status
               FROM guest_rfid_cards grc
               WHERE grc.guest_id = m.id
               ORDER BY grc.issue_time DESC
               LIMIT 1) AS latest_card_status
        FROM members m
        LEFT JOIN payments p ON m.id = p.member_id
        LEFT JOIN access_logs al ON al.actor_type = 'guest' AND al.actor_ref = m.rfid_uid
        WHERE m.member_type = 'guest'
        """

        params = []

        if q:
            base_query += " AND (m.full_name LIKE ? OR m.rfid_uid LIKE ? OR CAST(m.id AS TEXT) LIKE ?)"
            search_param = f"%{q}%"
            params.extend([search_param, search_param, search_param])

        if status_filter == "active":
            base_query += " AND (SELECT grc.status FROM guest_rfid_cards grc WHERE grc.guest_id = m.id ORDER BY grc.issue_time DESC LIMIT 1) = 'ACTIVE'"
        elif status_filter == "inactive":
            base_query += " AND (SELECT grc.status FROM guest_rfid_cards grc WHERE grc.guest_id = m.id ORDER BY grc.issue_time DESC LIMIT 1) != 'ACTIVE'"

        base_query += " GROUP BY m.id ORDER BY m.created_at DESC"

        guests = conn.execute(base_query, params).fetchall()

    # Create CSV
    output = StringIO()
    writer = csv.writer(output)

    # Write header
    writer.writerow([
        'Guest ID', 'Name', 'Contact', 'Address', 'RFID Card', 'Locker',
        'Status', 'Total Payments', 'Payment Count', 'Access Events',
        'Created Date', 'Last Access'
    ])

    # Write data
    for guest in guests:
        status = guest['latest_card_status'] or 'UNKNOWN'
        writer.writerow([
            guest['id'],
            guest['full_name'],
            guest['contact_number'] or '',
            guest['address'] or '',
            guest['rfid_uid'] or '',
            guest['locker_id'] or '',
            status,
            guest['total_payments'],
            guest['payment_count'],
            guest['total_access_events'],
            guest['created_at'][:19] if guest['created_at'] else '',
            guest['last_access_time'][:19] if guest['last_access_time'] else '',
        ])

    # Return CSV file
    output.seek(0)
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=guest_list.csv"
    response.headers["Content-type"] = "text/csv"
    return response


@admin_bp.post("/guests/create")
def admin_create_guest():
    """Create a guest with RFID card."""
    guard = _require_admin()
    if guard is not None:
        return json_error("unauthorized", "Admin login required.", 401)
    
    full_name = (request.form.get("full_name") or "").strip()
    rfid_uid = (request.form.get("rfid_uid") or "").strip() or None
    locker_id_raw = (request.form.get("locker_id") or "").strip()
    locker_id = None
    if locker_id_raw:
        try:
            locker_id = int(locker_id_raw)
        except ValueError:
            return json_error("invalid_locker_id", "Assigned locker must be a valid locker number.", 400)

    category = (request.form.get("category") or "regular").strip().lower() or "regular"
    payment_amount = 35 if category == "student" else 40

    if not full_name:
        return json_error("missing_full_name", "Guest name is required.", 400)

    try:
        with connect() as conn:
            existing = None
            if rfid_uid:
                # Check for duplicate RFID - but allow if previous guest's access has expired or been returned.
                existing = conn.execute(
                    "SELECT id, member_type, expiry_date FROM members WHERE rfid_uid = ?",
                    (rfid_uid,),
                ).fetchone()
                
                if existing and existing["member_type"] == "guest":
                    # Check the current card state for this RFID.
                    card_state = conn.execute(
                        "SELECT status FROM guest_rfid_cards WHERE rfid_uid = ? ORDER BY issue_time DESC LIMIT 1",
                        (rfid_uid,),
                    ).fetchone()
                    card_status = card_state["status"] if card_state else None

                    if card_status == "ACTIVE":
                        return json_error("rfid_in_use", "RFID card is currently in use.", 409)
                    if card_status in ("BLACKLISTED", "LOST"):
                        return json_error("rfid_blacklisted", "RFID card is blacklisted.", 409)

                    if card_status not in ("EXPIRED", "RETURNED"):
                        # Fallback to expiry date when the card record is not present.
                        if existing["expiry_date"]:
                            expiry = datetime.fromisoformat(existing["expiry_date"])
                            if datetime.now() < expiry:
                                return json_error("rfid_in_use", "RFID card is currently in use.", 409)
                        else:
                            return json_error("rfid_in_use", "RFID card is currently in use.", 409)

                    # Preserve the old guest record for history, but clear its active RFID and locker mapping.
                    old_locker_id = conn.execute(
                        "SELECT locker_id FROM members WHERE id = ?",
                        (existing["id"],),
                    ).fetchone()
                    if old_locker_id and old_locker_id["locker_id"]:
                        conn.execute(
                            "UPDATE lockers SET status = 'available' WHERE id = ?",
                            (old_locker_id["locker_id"],),
                        )
                        conn.execute(
                            "UPDATE members SET locker_id = NULL WHERE id = ?",
                            (existing["id"],),
                        )
                    conn.execute(
                        "UPDATE members SET rfid_uid = NULL WHERE id = ?",
                        (existing["id"],),
                    )
                elif existing and existing["member_type"] != "guest":
                    # Don't allow reusing member RFIDs
                    return json_error("rfid_in_use", "RFID card already in use by a member.", 409)

            # Check for role conflict: prevent creating guest if person already has fingerprint as member
            role_conflict = conn.execute(
                "SELECT id, member_type FROM members WHERE full_name = ? AND fingerprint_uid IS NOT NULL",
                (full_name,),
            ).fetchone()
            if role_conflict:
                return json_error("role_conflict", "Person already registered as member with fingerprint - cannot create as guest.", 409)

            # Check locker availability if locker assigned
            if locker_id:
                locker = conn.execute(
                    "SELECT status FROM lockers WHERE id = ?",
                    (locker_id,),
                ).fetchone()
                if not locker:
                    return json_error("locker_not_found", "Locker not found.", 404)

            # Guard against invalid admin IDs when this app stores admins separately from members.
            admin_id = session.get("admin_id")
            if admin_id is not None:
                valid_admin = conn.execute(
                    "SELECT id FROM members WHERE id = ?",
                    (admin_id,),
                ).fetchone()
                if not valid_admin:
                    admin_id = None

            # Create guest (instant access, paid automatically based on category)
            expiry_date = None
            cur = conn.execute(
                """
                INSERT INTO members (full_name, rfid_uid, locker_id, status, payment_status, 
                                    expiry_date, member_type, category, created_at)
                VALUES (?,?,?,'approved','paid',?,'guest',?,datetime('now'))
                """,
                (full_name, rfid_uid, locker_id, expiry_date, category),
            )
            guest_id = cur.lastrowid

            if rfid_uid:
                now = datetime.now()
                expires_at = ""
                expected_return_time = None
                conn.execute(
                    """
                    INSERT INTO guest_rfid_cards
                        (guest_id, rfid_uid, status, issue_time, expires_at, expected_return_time, checkout_admin_id, checkout_notes, locker_id)
                    VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guest_id,
                        rfid_uid,
                        now.strftime("%Y-%m-%d %H:%M:%S"),
                        expires_at,
                        expected_return_time,
                        admin_id,
                        "Guest created with RFID card",
                        locker_id,
                    ),
                )
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("admin", str(guest_id), "card_issued", f"guest_id={guest_id}; rfid_uid={rfid_uid}; locker_id={locker_id or 'N/A'}"),
                )

            # Update locker status if locker was assigned
            if locker_id:
                conn.execute(
                    "UPDATE lockers SET status = 'occupied' WHERE id = ?",
                    (locker_id,),
                )
            
            # Note: Locker status is only for IR sensor state (available/occupied)
            # Assignment is tracked via members.locker_id, not locker status
            
            # Log guest creation
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("admin", str(guest_id), "guest_created", f"guest_id={guest_id}; rfid={rfid_uid or 'N/A'}; expires={expiry_date or 'N/A'}"),
            )
            
            # Record payment
            conn.execute(
                "INSERT INTO payments (member_id, amount, payment_type, notes) VALUES (?, ?, ?, ?)",
                (guest_id, payment_amount, "guest", f"Guest payment: ₱{payment_amount}"),
            )
            
            conn.commit()

            return json_success({
                "status": "created",
                "guest_id": guest_id,
                "message": f"Guest '{full_name}' recorded with payment ₱{payment_amount}"
            }, 201)
    except Exception:
        import traceback
        traceback.print_exc()
        return json_error("server_error", "Unable to create guest access due to internal error.", 500)


@admin_bp.post("/delete-guest/<int:guest_id>")
def admin_delete_guest(guest_id):
    """Delete a guest record."""
    guard = _require_admin()
    if guard is not None:
        return {"error": "Unauthorized"}, 401

    with connect() as conn:
        # Get guest info before deletion
        guest = conn.execute(
            "SELECT * FROM members WHERE id = ? AND member_type = 'guest'",
            (guest_id,)
        ).fetchone()

        if not guest:
            return {"error": "Guest not found"}, 404

        # Free locker if assigned
        if guest['locker_id']:
            conn.execute(
                "UPDATE lockers SET status = 'available' WHERE id = ?",
                (guest['locker_id'],)
            )

        # Delete payments
        conn.execute("DELETE FROM payments WHERE member_id = ?", (guest_id,))

        # Delete access logs
        if guest['rfid_uid']:
            conn.execute(
                "DELETE FROM access_logs WHERE actor_type = 'guest' AND actor_ref = ?",
                (guest['rfid_uid'],)
            )

        # Delete guest RFID card record
        conn.execute("DELETE FROM guest_rfid_cards WHERE guest_id = ?", (guest_id,))

        # Delete guest
        conn.execute("DELETE FROM members WHERE id = ?", (guest_id,))

        # Log the deletion
        conn.execute(
            "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
            ("admin", "system", "guest_deleted", f"Guest {guest['full_name']} (ID: {guest_id}) deleted"),
        )

    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return {"success": True, "message": f"Guest {guest['full_name']} deleted successfully"}

    return redirect(url_for('admin.admin_rfid'))



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


@admin_bp.post("/settings/lockers/<int:locker_id>/<action>")
def admin_settings_lock_action(locker_id: int, action: str):
    guard = _require_admin()
    if guard is not None:
        return guard

    if action not in ("lock", "unlock"):
        return json_error("invalid_action", "Invalid locker action.", 400)

    with connect() as conn:
        locker_row = conn.execute(
            "SELECT id, label FROM lockers WHERE id = ?",
            (locker_id,),
        ).fetchone()

    if not locker_row:
        return json_error("not_found", "Locker not found.", 404)

    try:
        if action == "lock":
            state = device.lock(locker_id)
        else:
            state = device.unlock(locker_id)
    except requests.exceptions.RequestException as exc:
        return json_error("device_error", f"Failed to {action} locker: {exc}", 503)
    except Exception as exc:
        return json_error("device_error", f"Failed to {action} locker: {exc}", 500)

    with connect() as conn:
        conn.execute(
            "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
            (
                "admin",
                session.get("admin_username", "unknown"),
                f"locker_{action}",
                f"locker_id={locker_id}; action={action}",
            ),
        )
        conn.commit()

    return json_success(
        {
            "locker_id": locker_id,
            "action": action,
            "locked": state.locked,
            "item_detected": state.item_detected,
        }
    )


@admin_bp.get("/settings/lockers/status")
def admin_settings_locker_statuses():
    guard = _require_admin()
    if guard is not None:
        return guard

    lockers = _load_locker_statuses()
    return jsonify({"lockers": lockers})


@admin_bp.get("/settings")
def admin_settings():
    """Display system settings page."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    settings = load_settings()
    admin_users = [
        {
            "id": 1,
            "username": "admin",
            "email": "admin@example.com",
            "role": "Super Admin",
            "status": "Active",
        }
    ]

    lockers = []
    try:
        lockers = _load_locker_statuses()
    except Exception as exc:
        flask.current_app.logger.warning(
            "Error loading locker status for settings page: %s",
            exc,
        )

    return render_template(
        "admin/pages/admin_settings.html",
        settings=settings,
        lockers=lockers,
        current_page="settings",
        admin_username=session.get("admin_username"),
        now=datetime.now(),
        admin_users=admin_users,
        current_admin_id=session.get("admin_id", 1),
        master_unlock_text=MASTER_UNLOCK_CONFIRM_TEXT,
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
def admin_reports_redirect():
    """Redirect old reports route to the new analytics dashboard."""
    guard = _require_admin()
    if guard is not None:
        return guard
    return redirect(url_for("admin.admin_analytics"))


@admin_bp.get("/analytics")
def admin_analytics():
    """Render the analytics dashboard with charts and system metrics."""
    guard = _require_admin()
    if guard is not None:
        return guard
    
    with connect() as conn:
        total_members = conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'regular'").fetchone()["c"]
        total_guests = conn.execute("SELECT COUNT(*) AS c FROM members WHERE member_type = 'guest'").fetchone()["c"]
        active_guests = total_guests
        expired_guests = 0
        total_access_logs = conn.execute("SELECT COUNT(*) AS c FROM access_logs WHERE actor_type NOT IN ('system', 'ir_sensor')").fetchone()["c"]
        total_revenue = conn.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM payments").fetchone()["total"]
        
        # Daily revenue trend for the last 30 days
        daily_revenue = conn.execute(
            """
            SELECT date(payment_date) AS day, COALESCE(SUM(amount), 0) AS total
            FROM payments
            WHERE datetime(payment_date) >= datetime('now', '-29 days')
            GROUP BY date(payment_date)
            ORDER BY day ASC
            """
        ).fetchall()

        monthly_payment_count = conn.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE datetime(payment_date) >= datetime('now', '-29 days')"
        ).fetchone()["c"]
        
        # Guest category distribution
        guest_category_distribution = conn.execute(
            """
            SELECT category, COUNT(*) AS c
            FROM members
            WHERE member_type = 'guest'
            GROUP BY category
            """
        ).fetchall()

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
    
    return render_template(
        "admin/pages/admin_analytics.html",
        current_page="analytics",
        admin_username=session.get("admin_username"),
        stats={
            "total_members": total_members,
            "total_guests": total_guests,
            "active_guests": active_guests,
            "expired_guests": expired_guests,
            "total_access_logs": total_access_logs,
            "total_revenue": total_revenue,
            "monthly_revenue": sum(item["total"] for item in daily_revenue),
            "monthly_access_count": monthly_payment_count,
        },
        top_actions=top_actions,
        access_by_type=access_by_type,
        daily_revenue=[dict(row) for row in daily_revenue],
        guest_category_distribution=[dict(row) for row in guest_category_distribution],
    )


@admin_bp.get("/analytics/export-csv")
def export_analytics_csv():
    """Export analytics data as CSV."""
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


@admin_bp.get("/analytics/export-pdf")
def export_analytics_pdf():
    """Export analytics data as PDF."""
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
