from __future__ import annotations

from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, make_response, redirect, render_template, request, session, url_for
from requests.exceptions import RequestException

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    def load_dotenv_file(path: str = '.env') -> None:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
        except FileNotFoundError:
            pass

    load_dotenv_file()

try:
    from .db import connect, init_db
    from .device import get_device_controller
    from .device.background_jobs import start_background_jobs, stop_background_jobs, add_card_fee_to_payment
    from .admin import admin_bp, load_settings, verify_admin_credentials, ADMIN_USERNAME
except ImportError:
    from db import connect, init_db
    from device import get_device_controller
    from device.background_jobs import start_background_jobs, stop_background_jobs, add_card_fee_to_payment
    from admin import admin_bp, load_settings, verify_admin_credentials, ADMIN_USERNAME

# Global device controller instance - lazy initialized
device = None

# Locker assignments for separate member/guest access
MEMBER_LOCKER_IDS = (1, 2)
GUEST_LOCKER_IDS = (3, 4)


def json_error(error: str, message: str, status_code: int = 400):
    return make_response(jsonify({"error": error, "message": message}), status_code)


def json_success(payload: dict, status_code: int = 200):
    return make_response(jsonify(payload), status_code)


def is_member_locker(locker_id: int) -> bool:
    return locker_id in MEMBER_LOCKER_IDS


def is_guest_locker(locker_id: int) -> bool:
    return locker_id in GUEST_LOCKER_IDS


def get_device():
    global device
    if device is None:
        device = get_device_controller()
    return device

# Runtime enrollment state shared between browser and ESP32 polling
fingerprint_enrollment_state = {
    "pending": False,
    "enrolled_uid": None,
    "step": 0,
    "status": "waiting",
    "message": "Waiting for fingerprint enrollment",
    "error": None,
    "active": False,  # New: indicates if enrollment is currently active
}

# Runtime access status shared between the fingerprint scanner and the browser access page
access_status_state = {
    "state": "waiting",
    "locker_id": None,
    "member_id": None,
    "member_name": None,
    "message": "Awaiting fingerprint scan",
    "updated_at": None,
}

# Runtime master lock state shared between index page and admin actions
system_lock_state = {
    "locked": False,
    "locked_by": None,
    "locked_at": None,
}

# Runtime scan control - fingerprint scanning only enabled when user is actively on member access page
scan_control_state = {
    "enabled": False,
    "last_enabled": None,
    "timeout_seconds": 60,  # Disable scanning after 60 seconds of inactivity
}

def create_app() -> Flask:
    app = Flask(__name__)
    # Simple secret key for session handling (admin login). Replace in production.
    app.secret_key = "locker-secret-change-me"
    init_db()
    # device is now initialized globally

    # Register blueprints
    app.register_blueprint(admin_bp, url_prefix="/admin")

    # Track a single pending fingerprint enrollment command for ESP32 polling.
    # This avoids accidental enrollment starting unless the browser explicitly requested it.
    fingerprint_enrollment_state["pending"] = False
    fingerprint_enrollment_state["enrolled_uid"] = None
    fingerprint_enrollment_state["step"] = 0
    fingerprint_enrollment_state["error"] = None
    fingerprint_enrollment_state["active"] = False

    # Helper functions
    def _find_member_by_fingerprint(fp_uid: str):
        """Find MEMBER by fingerprint for locker access."""
        with connect() as conn:
            return conn.execute(
                "SELECT * FROM members WHERE fingerprint_uid = ? AND member_type = 'regular' AND status = 'approved' AND payment_status = 'paid'",
                (fp_uid,),
            ).fetchone()

    def _clear_registration_draft() -> None:
        session.pop("registration_draft", None)

    def _find_guest_by_rfid(rfid_uid: str):
        """Find ACTIVE guest card access for the given RFID."""
        with connect() as conn:
            card = conn.execute(
                """SELECT grc.*, m.id as guest_id, m.full_name, m.locker_id, m.expiry_date
                   FROM guest_rfid_cards grc
                   JOIN members m ON grc.guest_id = m.id
                   WHERE grc.rfid_uid = ?
                     AND grc.status = 'ACTIVE'
                   ORDER BY grc.issue_time DESC
                   LIMIT 1""",
                (rfid_uid,),
            ).fetchone()
            
            if card:
                card_dict = dict(card)
                card_dict['source'] = 'card'
                return card_dict
            
            # Fallback: check old style member-based guest RFID lookup
            member = conn.execute(
                "SELECT * FROM members WHERE rfid_uid = ? AND member_type = 'guest'",
                (rfid_uid,),
            ).fetchone()
            if member:
                member_dict = dict(member)
                member_dict['source'] = 'member'
                return member_dict
            return None

    def _find_guest_card_by_rfid(rfid_uid: str):
        """Find any guest RFID card record regardless of current status."""
        with connect() as conn:
            card = conn.execute(
                """SELECT grc.*, m.id as guest_id, m.full_name, m.locker_id, m.expiry_date
                   FROM guest_rfid_cards grc
                   JOIN members m ON grc.guest_id = m.id
                   WHERE grc.rfid_uid = ?
                   LIMIT 1""",
                (rfid_uid,),
            ).fetchone()
            return dict(card) if card else None

    def _find_member_by_fingerprint(fp_uid: str):
        """Find MEMBER by fingerprint for locker access."""
        with connect() as conn:
            return conn.execute(
                "SELECT * FROM members WHERE fingerprint_uid = ? AND member_type = 'regular' AND status = 'approved' AND payment_status = 'paid'",
                (fp_uid,),
            ).fetchone()

    @app.post("/device/rfid")
    def device_rfid():
        """Handle RFID card access for GUESTS ONLY (role security enforced)."""
        data = request.get_json(silent=True) or {}
        uid = (data.get("uid") or "").strip()
        if not uid:
            return {"error": "missing uid"}, 400

        # Find guest by RFID (checks card table first)
        guest_row = _find_guest_by_rfid(uid)
        guest = dict(guest_row) if guest_row else None

        if not guest:
            # If the RFID exists but is not currently active, return a more specific denial reason.
            stale_card = _find_guest_card_by_rfid(uid)
            if stale_card:
                reason = stale_card.get("status", "inactive")
                if reason == "ACTIVE":
                    reason = "card_inactive"
                elif reason == "EXPIRED":
                    reason = "card_expired"
                elif reason == "RETURNED":
                    reason = "card_returned"
                elif reason in ("BLACKLISTED", "LOST"):
                    reason = f"card_{reason.lower()}"
                else:
                    reason = f"card_{reason.lower()}"

                with connect() as conn:
                    conn.execute(
                        "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                        ("guest", str(stale_card.get("guest_id", stale_card.get("id"))), "access_denied", f"card_status={stale_card.get('status')}; rfid={uid}"),
                    )
                return {"status": "denied", "reason": reason}, 403

            # Check if this RFID belongs to a member (role violation)
            with connect() as conn:
                member_check = conn.execute(
                    "SELECT id, full_name FROM members WHERE rfid_uid = ? AND member_type = 'regular'",
                    (uid,),
                ).fetchone()
                
                if member_check:
                    # RFID belongs to a member - log security violation
                    conn.execute(
                        "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                        ("rfid", uid, "security_violation", f"member_rfid_used_as_guest; member_id={member_check['id']}; name={member_check['full_name']}"),
                    )
                    return {"status": "denied", "reason": "rfid_reserved_for_members"}, 403
            
            # RFID not found in system
            with connect() as conn:
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("rfid", uid, "access_denied", "unrecognized_rfid_or_not_guest"),
                )
            return {"status": "denied"}, 403

        guest_source = guest.get('source', 'card')

        if guest_source == 'card':
            from datetime import datetime

            if guest.get("status") not in ("ACTIVE", None, ""):
                reason = guest["status"].lower()
                with connect() as conn:
                    conn.execute(
                        "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                        ("guest", str(guest.get("guest_id", guest.get("id"))), "access_denied", f"card_status={guest['status']}; rfid={uid}"),
                    )
                return {"status": "denied", "reason": f"card_{reason}"}, 403

            if guest.get("expiry_date"):
                try:
                    member_expiry = datetime.fromisoformat(guest["expiry_date"])
                    if datetime.now() > member_expiry:
                        with connect() as conn:
                            conn.execute(
                                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                                ("guest", str(guest.get("guest_id", guest.get("id"))), "access_denied", f"guest_access_expired; rfid={uid}; expiry={guest['expiry_date']}"),
                            )
                        return {"status": "denied", "reason": "guest_access_expired"}, 403
                except (ValueError, TypeError):
                    pass
        else:
            from datetime import datetime
            if guest.get("expiry_date"):
                try:
                    member_expiry = datetime.fromisoformat(guest["expiry_date"])
                    if datetime.now() > member_expiry:
                        with connect() as conn:
                            conn.execute(
                                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                                ("guest", str(guest.get("id")), "access_denied", f"guest_access_expired; rfid={uid}; expiry={guest['expiry_date']}"),
                            )
                        return {"status": "denied", "reason": "guest_access_expired"}, 403
                except (ValueError, TypeError):
                    pass

        # Check if guest has locker assigned
        locker_id = guest.get("locker_id")
        if not locker_id:
            return {"status": "failed", "reason": "no locker assigned"}, 409

        # Enforce guest-only locker assignments.
        if locker_id not in GUEST_LOCKER_IDS:
            return {"status": "denied", "reason": "locker_not_guest_accessible"}, 403

        # Toggle locker state based on current device state.
        try:
            locker_state = get_device().get_locker(int(locker_id))
        except RequestException as exc:
            return {"status": "error", "reason": "device_unreachable", "message": str(exc)}, 503
        except Exception as exc:
            return {"status": "error", "reason": "device_unavailable", "message": str(exc)}, 500

        if locker_state.locked:
            new_state = "unlocked"
            try:
                get_device().unlock(int(locker_id))
            except RequestException as exc:
                return {"status": "error", "reason": "device_unreachable", "message": str(exc)}, 503
            except Exception as exc:
                return {"status": "error", "reason": "unlock_failed", "message": str(exc)}, 500
        else:
            new_state = "locked"
            try:
                get_device().lock(int(locker_id))
            except RequestException as exc:
                return {"status": "error", "reason": "device_unreachable", "message": str(exc)}, 503
            except Exception as exc:
                return {"status": "error", "reason": "lock_failed", "message": str(exc)}, 500

        with connect() as conn:
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("guest", str(guest.get("guest_id", guest.get("id"))), f"rfid_access_{new_state}", f"locker_id={locker_id}; rfid={uid}"),
            )

        return {"status": new_state, "locker_id": locker_id}

    @app.post("/device/fingerprint")
    def device_fingerprint():
        """Handle fingerprint access for MEMBERS ONLY (role security enforced)."""
        data = request.get_json(silent=True) or {}
        uid = (data.get("uid") or "").strip()
        if not uid:
            return {"error": "missing uid"}, 400

        # Check if fingerprint scanning is currently enabled
        last_enabled = scan_control_state.get("last_enabled")
        timeout_seconds = scan_control_state.get("timeout_seconds", 60)
        
        if not last_enabled or (datetime.now(timezone.utc) - last_enabled).total_seconds() >= timeout_seconds:
            print(f"[FINGERPRINT ACCESS] ✗ REJECTED - Fingerprint scanning not enabled (must use Member Access page)")
            return {"status": "rejected", "reason": "scanning_not_enabled"}, 403

        print(f"[FINGERPRINT ACCESS] Received fingerprint UID: {uid}")

        # Find member by fingerprint (must be approved and paid)
        member_row = _find_member_by_fingerprint(uid)
        member = dict(member_row) if member_row else None
        
        if not member:
            # Check if this fingerprint belongs to a guest (role violation)
            with connect() as conn:
                guest_check = conn.execute(
                    "SELECT id, full_name FROM members WHERE fingerprint_uid = ? AND member_type = 'guest'",
                    (uid,),
                ).fetchone()
                
                if guest_check:
                    # Fingerprint belongs to a guest - log security violation
                    conn.execute(
                        "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                        ("fingerprint", uid, "security_violation", f"guest_fingerprint_used_as_member; guest_id={guest_check['id']}; name={guest_check['full_name']}"),
                    )
                    return {"status": "denied", "reason": "fingerprint_reserved_for_guests"}, 403
            
            # Fingerprint not found in system
            with connect() as conn:
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("fingerprint", uid, "access_denied", "unrecognized_fingerprint_or_not_registered"),
                )
            print(f"[FINGERPRINT ACCESS] Fingerprint UID {uid} NOT FOUND or not registered for member access")
            # Store access status for polling in shared server state
            access_status_state["state"] = "denied"
            access_status_state["locker_id"] = None
            access_status_state["member_id"] = None
            access_status_state["member_name"] = None
            access_status_state["message"] = "Fingerprint not registered for member access"
            access_status_state["updated_at"] = datetime.now(timezone.utc)
            return {"status": "denied"}, 403

        locker_id = member["locker_id"]
        if not locker_id:
            return {"status": "failed", "reason": "no locker assigned"}, 409

        if not is_member_locker(int(locker_id)):
            print(f"[FINGERPRINT ACCESS] ✗ Member locker assignment invalid for locker {locker_id}")
            return {"status": "denied", "reason": "locker_not_member_accessible"}, 403

        print(f"[FINGERPRINT ACCESS] ✓ Member {member['full_name']} (ID={member['id']}) matched - unlocking locker {locker_id}")
        try:
            get_device().unlock(int(locker_id))
        except RequestException as exc:
            print(f"[FINGERPRINT ACCESS] ✗ ESP32 connection error: {exc}")
            access_status_state["state"] = "error"
            access_status_state["locker_id"] = locker_id
            access_status_state["member_id"] = member["id"]
            access_status_state["member_name"] = member["full_name"]
            access_status_state["message"] = "ESP32 device not reachable"
            access_status_state["updated_at"] = datetime.now(timezone.utc)
            return {"status": "error", "reason": "device_unreachable", "message": "ESP32 device not reachable"}, 503
        except Exception as exc:
            print(f"[FINGERPRINT ACCESS] ✗ ESP32 unlock error: {exc}")
            access_status_state["state"] = "error"
            access_status_state["locker_id"] = locker_id
            access_status_state["member_id"] = member["id"]
            access_status_state["member_name"] = member["full_name"]
            access_status_state["message"] = "ESP32 unlock command failed"
            access_status_state["updated_at"] = datetime.now(timezone.utc)
            return {"status": "error", "reason": "device_error", "message": str(exc)}, 502
        with connect() as conn:
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("member", str(member["id"]), "fingerprint_access_granted", f"locker_id={locker_id}; fingerprint={uid}"),
            )
        # Store access status for polling in shared server state
        access_status_state["state"] = "unlocked"
        access_status_state["locker_id"] = locker_id
        access_status_state["member_id"] = member["id"]
        access_status_state["member_name"] = member["full_name"]
        access_status_state["message"] = f"Welcome {member['full_name']}. Locker {locker_id} unlocked."
        access_status_state["updated_at"] = datetime.now(timezone.utc)
        return {"status": "unlocked", "locker_id": locker_id}

    @app.post("/device/fingerprint/enroll")
    def device_fingerprint_enroll():
        """Handle fingerprint enrollment during registration."""
        data = request.get_json(silent=True) or {}
        uid = (data.get("uid") or "").strip()
        step = data.get("step", 1)  # 1=first scan, 2=second scan/enrolled
        
        if not uid:
            return {"error": "missing uid"}, 400

        if step == 1:
            # First scan completed
            fingerprint_enrollment_state["step"] = 1
            fingerprint_enrollment_state["status"] = "first_scan"
            fingerprint_enrollment_state["message"] = "First scan registered. Remove and place your finger again for step 2."
            fingerprint_enrollment_state["error"] = None
            print(f"First fingerprint scan completed, uid={uid}")
            return {"status": "first_scan_complete", "fingerprint_uid": uid, "message": fingerprint_enrollment_state["message"]}
        else:
            # Check for duplicate fingerprint before accepting enrollment
            with connect() as conn:
                existing = conn.execute("SELECT id, full_name FROM members WHERE fingerprint_uid = ?", (uid,)).fetchone()
                if existing:
                    fingerprint_enrollment_state["status"] = "error"
                    fingerprint_enrollment_state["message"] = f"Fingerprint already registered to {existing[1]}."
                    fingerprint_enrollment_state["error"] = fingerprint_enrollment_state["message"]
                    print(f"Duplicate fingerprint detected, uid={uid}, existing user: {existing[1]}")
                    return {"error": "duplicate_fingerprint", "message": fingerprint_enrollment_state["message"]}, 409
            
            # Enrollment completed (second scan)
            fingerprint_enrollment_state["enrolled_uid"] = uid
            fingerprint_enrollment_state["pending"] = False
            fingerprint_enrollment_state["active"] = False  # Clear active flag
            fingerprint_enrollment_state["step"] = 2
            fingerprint_enrollment_state["status"] = "enrolled"
            fingerprint_enrollment_state["message"] = "Fingerprint enrolled successfully. Complete registration to finish."
            fingerprint_enrollment_state["error"] = None
            print(f"Fingerprint enrolled from device, uid={uid}")

            with connect() as conn:
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("system", "enrollment", "fingerprint_enrolled", f"fingerprint_uid={uid}"),
                )
            
            return {"status": "enrolled", "fingerprint_uid": uid}

    def _reset_enrollment_state():
        fingerprint_enrollment_state["pending"] = False
        fingerprint_enrollment_state["enrolled_uid"] = None
        fingerprint_enrollment_state["step"] = 0
        fingerprint_enrollment_state["status"] = "waiting"
        fingerprint_enrollment_state["message"] = "Waiting for fingerprint enrollment"
        fingerprint_enrollment_state["error"] = None
        fingerprint_enrollment_state["active"] = False

    @app.post("/device/fingerprint/request-enrollment")
    def device_fingerprint_request_enrollment():
        """Request fingerprint enrollment mode for the ESP32."""
        data = request.get_json(silent=True) or {}
        action = data.get("action", "start")
        
        if action == "cancel":
            # Cancel enrollment - reset all states
            _reset_enrollment_state()
            print("Enrollment cancelled by user")
            return json_success({"success": True, "status": "cancelled", "message": "Enrollment cancelled"})
        
        # Reset any previous enrollment state first
        _reset_enrollment_state()
        
        fingerprint_enrollment_state["pending"] = True
        fingerprint_enrollment_state["enrolled_uid"] = None
        fingerprint_enrollment_state["step"] = 0
        fingerprint_enrollment_state["status"] = "waiting"
        fingerprint_enrollment_state["message"] = "Please place your finger on the scanner."
        fingerprint_enrollment_state["error"] = None
        fingerprint_enrollment_state["active"] = False
        print("Frontend requested enrollment - flag set to True")
        print(f"Current enrollment state: pending={fingerprint_enrollment_state['pending']}, enrolled_uid={fingerprint_enrollment_state['enrolled_uid']}")

        direct_start_failed = False
        direct_start_error = None
        try:
            device = get_device()
            if hasattr(device, "start_fingerprint_enrollment"):
                response = device.start_fingerprint_enrollment()
                print(f"ESP32 direct start enrollment response: {response}")
                if isinstance(response, dict):
                    response_status = response.get("status")
                    response_success = response.get("success")
                    response_started = response.get("enrollment_started")
                    if response_success is True or response_started is True or response_status in {
                        "enrollment_started",
                        "already_active",
                        "active",
                        "started",
                    }:
                        fingerprint_enrollment_state["pending"] = False
                        fingerprint_enrollment_state["active"] = True
                        fingerprint_enrollment_state["status"] = "waiting"
                        fingerprint_enrollment_state["message"] = (
                            "Scanner ready. Place your finger on the scanner."
                            if response_status != "already_active"
                            else "Scanner already active. Place your finger on the scanner."
                        )
                        print("ESP32 direct enrollment command accepted and active=True")
                        return json_success({"success": True, "message": "Enrollment started", "status": "pending"})

                direct_start_failed = True
                if isinstance(response, dict):
                    direct_start_error = response.get("message") or response.get("error") or "ESP32 failed to start enrollment"
                else:
                    direct_start_error = "ESP32 failed to start enrollment"
                print(f"[ESP32] enrollment start rejected: {direct_start_error}")
        except Exception as exc:
            direct_start_failed = True
            direct_start_error = f"ESP32 direct start enrollment failed: {type(exc).__name__}: {exc}"
            print(f"[ESP32] {direct_start_error}")

        if direct_start_failed:
            fingerprint_enrollment_state["status"] = "waiting"
            fingerprint_enrollment_state["message"] = "Enrollment queued. Waiting for the scanner to poll the backend."
            fingerprint_enrollment_state["error"] = direct_start_error
            fingerprint_enrollment_state["active"] = False
            print(f"Enrollment request queued despite direct start failure: {direct_start_error}")
            return json_success({
                "success": True,
                "message": "Enrollment started",
                "status": "pending",
                "note": "Device may begin enrollment when it next polls the backend.",
            })

        return json_success({"success": True, "message": "Enrollment started", "status": "pending"})

    @app.route("/device/fingerprint/start-enrollment", methods=["GET", "POST"])
    def device_fingerprint_start_enrollment():
        """Check if enrollment should be started (polled by ESP32)."""
        data = request.get_json(silent=True) or {}
        action = request.values.get("action", data.get("action", "check"))
        
        if action == "stop":
            # ESP32 is stopping enrollment
            fingerprint_enrollment_state["active"] = False
            fingerprint_enrollment_state["pending"] = False
            print("ESP32 stopped enrollment")
            return {"status": "enrollment_stopped"}
        
        pending = fingerprint_enrollment_state.get("pending", False)
        print(f"ESP32 polling for enrollment - pending: {pending}")
        if pending:
            fingerprint_enrollment_state["pending"] = False  # Clear the flag
            fingerprint_enrollment_state["active"] = True   # Set active flag
            print("Returning enrollment_started to ESP32")
            return {"status": "enrollment_started"}
        print("Returning no_action to ESP32")
        return {"status": "no_action"}

    @app.get("/api/enrollment-status")
    def api_enrollment_status():
        """Check if fingerprint has been enrolled during registration."""
        enrolled_uid = fingerprint_enrollment_state.get("enrolled_uid")
        enrollment_step = fingerprint_enrollment_state.get("step", 0)
        enrollment_status = fingerprint_enrollment_state.get("status", "waiting")
        enrollment_message = fingerprint_enrollment_state.get("message")
        enrollment_error = fingerprint_enrollment_state.get("error")
        enrollment_active = fingerprint_enrollment_state.get("active", False)
        
        return {
            "enrolled": bool(enrolled_uid), 
            "fingerprint_uid": enrolled_uid,
            "step": enrollment_step,  # 0=waiting, 1=first scan done, 2=enrolled
            "status": enrollment_status,
            "message": enrollment_message,
            "error": enrollment_error,
            "active": enrollment_active  # New: indicates if enrollment is currently active
        }

    @app.get("/api/access-status")
    def api_access_status():
        """Check for recent fingerprint access attempts."""
        updated_at = access_status_state.get("updated_at")
        if updated_at:
            age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
            if age_seconds < 30:
                return {
                    "state": access_status_state.get("state", "waiting"),
                    "locker_id": access_status_state.get("locker_id"),
                    "member_id": access_status_state.get("member_id"),
                    "member_name": access_status_state.get("member_name"),
                    "message": access_status_state.get("message", "Awaiting fingerprint scan"),
                }

        # Clear stale status after 30 seconds.
        access_status_state["state"] = "waiting"
        access_status_state["locker_id"] = None
        access_status_state["member_id"] = None
        access_status_state["member_name"] = None
        access_status_state["message"] = "Awaiting fingerprint scan"
        access_status_state["updated_at"] = None
        return {"state": "waiting", "locker_id": None, "member_id": None, "member_name": None, "message": "Awaiting fingerprint scan"}

    @app.post("/api/access/enable-scan")
    def api_enable_scan():
        """Enable fingerprint scanning when user enters member access page."""
        scan_control_state["enabled"] = True
        scan_control_state["last_enabled"] = datetime.now(timezone.utc)
        return {"status": "scan_enabled"}

    @app.post("/api/access/disable-scan")
    def api_disable_scan():
        """Disable fingerprint scanning when user leaves member access page."""
        scan_control_state["enabled"] = False
        scan_control_state["last_enabled"] = None
        return {"status": "scan_disabled"}

    @app.post("/api/access/clear-locker-state")
    def api_clear_locker_state():
        """Clear locker access state when user returns to home page."""
        access_status_state["state"] = "waiting"
        access_status_state["locker_id"] = None
        access_status_state["member_id"] = None
        access_status_state["member_name"] = None
        access_status_state["message"] = "Awaiting fingerprint scan"
        access_status_state["updated_at"] = None
        return {"status": "locker_state_cleared"}

    @app.post("/api/access/locker-action")
    def api_access_locker_action():
        """Lock or unlock the current member's assigned locker.
        Can use either session state OR locker_id from request body."""
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "").strip().lower()
        locker_id = data.get("locker_id")  # Can be provided in request
        
        if action not in ("lock", "unlock"):
            return {"error": "invalid_action"}, 400

        # Try to get locker_id from request first (for post-access page)
        # Fall back to session state (for inline access page)
        if not locker_id:
            locker_id = access_status_state.get("locker_id")
        
        if not locker_id:
            return {"error": "no_locker_id"}, 400

        member_id = access_status_state.get("member_id", "unknown")
        member_name = access_status_state.get("member_name", "Member")
        
        print(f"[LOCKER ACTION] Requested {action} for locker {locker_id} by member {member_id}")
        try:
            if action == "lock":
                get_device().lock(int(locker_id))
                new_state = "locked"
                message = f"Locker {locker_id} locked. Scan next member or unlock again."
                log_action = "member_locker_locked"
            else:
                get_device().unlock(int(locker_id))
                new_state = "unlocked"
                message = f"Locker {locker_id} unlocked. Scan next member or lock again."
                log_action = "member_locker_unlocked"
        except RequestException as e:
            print(f"[LOCKER ACTION] ✗ ESP32 connection error: {type(e).__name__}: {e}")
            with connect() as conn:
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("member", str(member_id), f"member_locker_{action}_failed", f"locker_id={locker_id}; member={member_name}; error={str(e)}"),
                )
            return {"status": "error", "reason": "device_unreachable", "message": str(e)}, 503
        except Exception as e:
            print(f"[LOCKER ACTION] ✗ Failed to {action} locker {locker_id}: {type(e).__name__}: {e}")
            with connect() as conn:
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("member", str(member_id), f"member_locker_{action}_failed", f"locker_id={locker_id}; member={member_name}; error={str(e)}"),
                )
            return {"status": "error", "action": action, "error": f"Failed to {action} locker: {type(e).__name__}"}, 500

        with connect() as conn:
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("member", str(member_id), log_action, f"locker_id={locker_id}; member={member_name}"),
            )

        access_status_state["state"] = new_state
        access_status_state["message"] = message
        access_status_state["updated_at"] = datetime.now(timezone.utc)
        print(f"[LOCKER ACTION] ✓ Successfully {action}ed locker {locker_id}")
        return {"status": "ok", "action": action, "locker_id": locker_id, "state": new_state}

    @app.get("/api/access/scan-enabled")
    def api_scan_enabled():
        """Check if fingerprint scanning is currently enabled."""
        last_enabled = scan_control_state.get("last_enabled")
        timeout_seconds = scan_control_state.get("timeout_seconds", 60)
        
        if last_enabled:
            elapsed = (datetime.now(timezone.utc) - last_enabled).total_seconds()
            if elapsed < timeout_seconds:
                # Still within timeout period - scanning enabled
                return {"enabled": True, "elapsed_seconds": int(elapsed)}

        # Timeout reached or never enabled - disable scanning
        scan_control_state["enabled"] = False
        scan_control_state["last_enabled"] = None
        return {"enabled": False, "elapsed_seconds": 0}

    @app.post("/device/ir-status")
    def device_ir_status():
        """Handle IR sensor occupancy status updates."""
        data = request.get_json(silent=True) or {}
        uid = (data.get("uid") or "").strip()
        status = (data.get("status") or "").strip()
        
        if not uid or not status:
            return {"error": "missing uid or status"}, 400
        
        if status not in ["occupied", "available"]:
            return {"error": "invalid status (must be 'occupied' or 'available')"}, 400
        
        # Extract locker number from uid (e.g., "locker_1" → 1)
        try:
            locker_id = int(uid.split("_")[-1])
        except (ValueError, IndexError):
            return {"error": "invalid uid format"}, 400
        
        # Ignore IR occupancy events for member lockers entirely.
        # Only guest lockers 3 and 4 are instrumented with IR sensors.
        if not is_guest_locker(locker_id):
            return {"success": True, "uid": uid, "status": status, "ignored": True}

        # Update guest locker IR status in database
        with connect() as conn:
            conn.execute(
                "UPDATE lockers SET status = ? WHERE id = ?",
                (status, locker_id),
            )
            conn.commit()
        
        return {"success": True, "uid": uid, "status": status}

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            system_locked=system_lock_state["locked"],
            lock_by=system_lock_state["locked_by"],
            lock_at=system_lock_state["locked_at"],
        )

    @app.route("/api/system-lock", methods=["POST"])
    def api_system_lock():
        data = request.get_json(silent=True) or {}
        password = (data.get("password") or "").strip()
        if not password:
            return {"error": "missing password"}, 400

        admin_username = session.get("admin_username", ADMIN_USERNAME)
        if not verify_admin_credentials(admin_username, password):
            return {"error": "invalid credentials"}, 403

        if system_lock_state["locked"]:
            return {"status": "locked", "message": "System is already locked."}, 200

        locker_ids = list(MEMBER_LOCKER_IDS) + list(GUEST_LOCKER_IDS)
        lock_errors = []

        for locker_id in locker_ids:
            try:
                get_device().lock(int(locker_id))
            except RequestException as exc:
                lock_errors.append(f"locker_{locker_id}: {type(exc).__name__}")
            except Exception as exc:
                lock_errors.append(f"locker_{locker_id}: {type(exc).__name__} {exc}")

        if lock_errors:
            return {
                "status": "error",
                "error": "device_lock_failure",
                "message": "Failed to activate system lock on one or more lockers.",
                "details": lock_errors,
            }, 503

        system_lock_state["locked"] = True
        system_lock_state["locked_by"] = admin_username
        system_lock_state["locked_at"] = datetime.now(timezone.utc).isoformat()

        with connect() as conn:
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                (
                    "admin",
                    admin_username,
                    "system_locked",
                    f"locked_by={admin_username}; locked_at={system_lock_state['locked_at']}",
                ),
            )
            conn.commit()

        return {
            "status": "locked",
            "locked_by": system_lock_state["locked_by"],
            "locked_at": system_lock_state["locked_at"],
            "message": "System locked successfully.",
        }, 200

    @app.route("/api/system-unlock", methods=["POST"])
    def api_system_unlock():
        data = request.get_json(silent=True) or {}
        password = (data.get("password") or "").strip()
        if not password:
            return {"error": "missing password"}, 400

        admin_username = session.get("admin_username", ADMIN_USERNAME)
        if not verify_admin_credentials(admin_username, password):
            return {"error": "invalid credentials"}, 403

        if not system_lock_state["locked"]:
            return {"status": "unlocked", "message": "System is already unlocked."}, 200

        system_lock_state["locked"] = False
        system_lock_state["locked_by"] = None
        system_lock_state["locked_at"] = None

        with connect() as conn:
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                (
                    "admin",
                    admin_username,
                    "system_unlocked",
                    f"unlocked_by={admin_username}",
                ),
            )
            conn.commit()

        return {
            "status": "unlocked",
            "message": "System unlocked successfully.",
        }, 200

    @app.get("/static/favicon.ico")
    def favicon():
        return app.send_static_file("favicon.ico")

    @app.get("/user/register")
    def user_register_form():
        error = session.pop("registration_error", None)
        return render_template("pages/user_register.html", error=error)

    @app.post("/user/register")
    def user_register_submit():
        full_name = (request.form.get("full_name") or "").strip()
        address = (request.form.get("address") or "").strip()
        contact_number = (request.form.get("contact_number") or "").strip()
        age_raw = (request.form.get("age") or "").strip()
        category = (request.form.get("category") or "").strip().lower()
        error = None

        if not full_name:
            error = "Full name is required."
        elif not address:
            error = "Address is required."
        elif not contact_number:
            error = "Contact number is required."
        elif not age_raw:
            error = "Age is required."
        elif not category:
            error = "Category is required."

        age_val = None
        if not error:
            try:
                age_val = int(age_raw)
                if age_val <= 0:
                    raise ValueError
            except ValueError:
                error = "Age must be a positive number."

        if not error:
            normalized_contact = ''.join(ch for ch in contact_number if ch.isdigit())
            if len(normalized_contact) < 10:
                error = "Contact number must contain at least 10 digits."
            elif normalized_contact != contact_number:
                error = "Contact number may only contain digits."

        valid_categories = {"student", "adult", "senior"}
        if not error and category not in valid_categories:
            error = "Please select a valid category."

        if error:
            return render_template(
                "pages/user_register.html",
                error=error,
                form_data={
                    "full_name": full_name,
                    "address": address,
                    "contact_number": contact_number,
                    "age": age_raw,
                    "category": category,
                },
            )

        # Stage details in session; do not save to DB until fingerprint enrollment completes.
        session["registration_draft"] = {
            "full_name": full_name,
            "address": address,
            "contact_number": contact_number,
            "age": age_val,
            "category": category,
            "locker_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

        return redirect(url_for("user_select_locker"))

    @app.get("/user/select-locker")
    def user_select_locker():
        draft = session.get("registration_draft")
        if not isinstance(draft, dict) or not draft.get("full_name"):
            return redirect(url_for("user_register_form"))

        with connect() as conn:
            # Only member-assigned locker IDs may be chosen during member registration.
            lockers = conn.execute(
                """SELECT id, label,
                   CASE WHEN id IN (1, 2) THEN 'member' ELSE 'guest' END AS locker_type,
                   CASE WHEN EXISTS(
                     SELECT 1 FROM members 
                     WHERE locker_id = lockers.id 
                     AND member_type = 'regular' 
                     AND status = 'approved'
                   ) THEN 'member_assigned' 
                   WHEN EXISTS(
                     SELECT 1 FROM members 
                     WHERE locker_id = lockers.id 
                     AND member_type = 'guest' 
                     AND expiry_date > datetime('now', 'localtime')
                   ) THEN 'guest_assigned' 
                   ELSE NULL END as assignment_status
                   FROM lockers ORDER BY id LIMIT 4"""
            ).fetchall()

        return render_template(
            "pages/user_select_locker.html",
            draft=draft,
            lockers=lockers,
        )

    @app.post("/user/select-locker")
    def user_select_locker_submit():
        locker_id_raw = (request.form.get("locker_id") or "").strip()
        draft = session.get("registration_draft")
        if not isinstance(draft, dict) or not draft.get("full_name"):
            return redirect(url_for("user_register_form"))
        if not locker_id_raw.isdigit():
            return redirect(url_for("user_select_locker"))
        locker_id = int(locker_id_raw)

        with connect() as conn:
            if locker_id not in MEMBER_LOCKER_IDS:
                return redirect(url_for("user_select_locker"))

            # Ensure locker is available and not already assigned to any approved member or active guest
            locker = conn.execute(
                """SELECT id FROM lockers WHERE id = ? 
                   AND NOT EXISTS(
                     SELECT 1 FROM members 
                     WHERE locker_id = ? 
                     AND (
                       (member_type = 'regular' AND status = 'approved')
                       OR (member_type = 'guest' AND expiry_date > datetime('now', 'localtime'))
                     )
                   )""",
                (locker_id, locker_id),
            ).fetchone()
            if locker is None:
                return redirect(url_for("user_select_locker"))

        draft["locker_id"] = locker_id
        session["registration_draft"] = draft
        return redirect(url_for("user_enroll_fingerprint"))

    @app.get("/user/enroll-fingerprint")
    def user_enroll_fingerprint():
        draft = session.get("registration_draft")
        if not isinstance(draft, dict) or not draft.get("full_name"):
            return redirect(url_for("user_register_form"))
        if not draft.get("locker_id"):
            return redirect(url_for("user_select_locker"))
        with connect() as conn:
            locker = conn.execute(
                "SELECT id, label FROM lockers WHERE id = ?",
                (int(draft["locker_id"]),),
            ).fetchone()
        return render_template("pages/user_enroll_fingerprint.html", draft=draft, locker=locker)

    @app.post("/user/enroll-fingerprint/complete")
    def user_enroll_fingerprint_complete():
        """
        Finalize registration ONLY after fingerprint enrollment succeeds.
        For now this is a 'complete' action; later wire to the real R307 enrollment result.
        """
        draft = session.get("registration_draft")
        if not isinstance(draft, dict) or not draft.get("full_name") or not draft.get("locker_id"):
            return redirect(url_for("user_register_form"))

        # Get fingerprint_uid from shared enrollment result state
        fingerprint_uid = fingerprint_enrollment_state.get("enrolled_uid")
        if not fingerprint_uid:
            # If no fingerprint enrolled yet, redirect back to enrollment page
            return redirect(url_for("user_enroll_fingerprint"))
        
        with connect() as conn:
            # Prevent duplicate fingerprint registration across different users.
            existing_fingerprint = conn.execute(
                "SELECT id, full_name FROM members WHERE fingerprint_uid = ?",
                (fingerprint_uid,),
            ).fetchone()
            if existing_fingerprint:
                session["registration_error"] = (
                    "Ang fingerprint na ito ay naka-rehistro na sa ibang user. "
                    "Kung sa tingin mo ay mali, kontakin ang admin."
                )
                _clear_registration_draft()
                return redirect(url_for("user_register_form"))

            # Check for role conflict: prevent registering as member if person already has RFID as guest
            role_conflict = conn.execute(
                "SELECT id, member_type FROM members WHERE full_name = ? AND rfid_uid IS NOT NULL",
                (draft.get("full_name"),),
            ).fetchone()
            if role_conflict:
                # Log the role violation attempt
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("system", "unknown", "role_violation_attempt", f"name={draft.get('full_name')}; attempted=member_registration; conflict=guest_with_rfid"),
                )
                session["registration_error"] = "You are already registered as a guest with RFID access. Please contact admin if you need to change your access method."
                _clear_registration_draft()
                return redirect(url_for("user_register_form"))

            # Check if this person already has a pending registration
            existing = conn.execute(
                """
                SELECT id, status FROM members 
                WHERE full_name = ? AND (status = 'pending' OR status = 'approved')
                LIMIT 1
                """,
                (draft.get("full_name"),),
            ).fetchone()
            
            if existing:
                # Already registered, redirect to success or show message
                session["registration_success"] = {
                    "member_id": existing["id"],
                    "member_name": draft.get("full_name"),
                    "is_duplicate": True,
                }
                _clear_registration_draft()
                return redirect(url_for("user_registered_success"))
            
            # Create member with the locker they selected (pending approval)
            cur = conn.execute(
                """
                INSERT INTO members (full_name, address, contact_number, age, category, locker_id, fingerprint_uid, 
                                     status, payment_status, expiry_date, member_type)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    draft.get("full_name"),
                    draft.get("address"),
                    draft.get("contact_number"),
                    draft.get("age"),
                    draft.get("category"),
                    draft.get("locker_id"),  # Store the locker they selected during registration
                    fingerprint_uid if fingerprint_uid else None,
                    "pending",  # Status: pending approval
                    "unpaid",  # Payment: unpaid until admin confirms
                    (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),  # Membership valid 30 days after approval
                    "regular",  # Member type
                ),
            )
            member_id = cur.lastrowid
            
            # Log registration with selected locker
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("system", str(member_id), "member_registered", f"member_id={member_id}; status=pending; locker_id={draft.get('locker_id')}"),
            )
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("system", str(member_id), "fingerprint_enrolled", "r307=enrolled"),
            )

        # Store success data in session for the success page
        session["registration_success"] = {
            "member_id": member_id,
            "member_name": draft.get("full_name"),
        }
        _clear_registration_draft()
        session.pop("enrolled_fingerprint_uid", None)
        fingerprint_enrollment_state["enrolled_uid"] = None
        return redirect(url_for("user_registered_success"))

    @app.get("/user/registered-success")
    def user_registered_success():
        success_data = session.get("registration_success")
        if not success_data:
            return redirect(url_for("index"))
        return render_template(
            "pages/user_registered_success.html",
            member_name=success_data.get("member_name"),
            is_duplicate=success_data.get("is_duplicate", False),
        )

    @app.post("/user/register/cancel")
    def user_register_cancel():
        _clear_registration_draft()
        return redirect(url_for("index"))

    @app.get("/user/access")
    def user_access():
        # Enable fingerprint scanning whenever the member access page is rendered.
        scan_control_state["enabled"] = True
        scan_control_state["last_enabled"] = datetime.now(timezone.utc)
        return render_template("pages/user_access.html")

    @app.get("/user/locker-access")
    def user_locker_access():
        """Display the improved locker access confirmation page."""
        return render_template("pages/member_locker.html")

    # ========== ADMIN ENDPOINTS - Card Management & Emergency Controls ==========
    
    @app.post("/api/admin/card-issue")
    def admin_card_issue():
        """Admin endpoint to issue a new RFID card to a guest."""
        data = request.get_json(silent=True) or {}
        guest_id = data.get("guest_id")
        rfid_uid = (data.get("rfid_uid") or "").strip()
        hours_valid = 24
        admin_id = data.get("admin_id") or session.get("admin_id")
        checkout_notes = (data.get("notes") or "").strip()
        
        try:
            guest_id = int(guest_id)
        except (TypeError, ValueError):
            guest_id = None
        
        if not guest_id or not rfid_uid:
            return {"error": "missing or invalid guest_id or rfid_uid"}, 400
        
        from datetime import datetime, timedelta
        
        with connect() as conn:
            # Verify guest exists
            guest = conn.execute(
                "SELECT id, full_name, locker_id FROM members WHERE id = ? AND member_type = 'guest'",
                (guest_id,),
            ).fetchone()
            
            if not guest:
                return {"error": "guest_not_found"}, 404
            
            # Check if card already exists in system
            existing = conn.execute(
                "SELECT id, status FROM guest_rfid_cards WHERE rfid_uid = ? ORDER BY issue_time DESC LIMIT 1",
                (rfid_uid,),
            ).fetchone()
            
            if existing:
                existing_status = existing["status"] or "UNKNOWN"
                if existing_status == "ACTIVE":
                    return {"error": "card_already_registered", "rfid_uid": rfid_uid}, 409
                if existing_status in ("BLACKLISTED", "LOST"):
                    return {"error": "card_blacklisted", "rfid_uid": rfid_uid, "status": existing_status}, 409
                if existing_status not in ("EXPIRED", "RETURNED"):
                    return {"error": "card_invalid_status", "rfid_uid": rfid_uid, "status": existing_status}, 409
            
            now = datetime.now()
            issue_time = now.isoformat()
            expires_at = ""
            expected_return = None
            
            if existing and existing["status"] in ("EXPIRED", "RETURNED"):
                conn.execute(
                    """INSERT INTO guest_rfid_cards
                       (guest_id, rfid_uid, status, issue_time, expires_at, expected_return_time, checkout_admin_id, checkout_notes, locker_id)
                       VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?)""",
                    (
                        guest_id,
                        rfid_uid,
                        issue_time,
                        expires_at,
                        expected_return,
                        admin_id,
                        checkout_notes or "Card issued by admin",
                        guest.get("locker_id"),
                    ),
                )
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("admin", str(admin_id or "unknown"), "card_reissued", f"guest_id={guest_id}; rfid_uid={rfid_uid}"),
                )
                return {
                    "status": "ok",
                    "message": f"Card reissued to {guest['full_name']}",
                    "rfid_uid": rfid_uid,
                }
            
            # Create card record
            conn.execute(
                """INSERT INTO guest_rfid_cards 
                   (guest_id, rfid_uid, status, issue_time, expires_at, expected_return_time, checkout_admin_id, checkout_notes, locker_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guest_id,
                    rfid_uid,
                    "ACTIVE",
                    issue_time,
                    expires_at,
                    expected_return,
                    admin_id,
                    checkout_notes or "Card issued by admin",
                    guest.get("locker_id"),
                ),
            )
            
            # Log the card issuance
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("admin", str(admin_id or "unknown"), "card_issued", f"guest_id={guest_id}; rfid_uid={rfid_uid}"),
            )
        
        return {
            "status": "ok", 
            "message": f"Card issued to {guest['full_name']}", 
            "rfid_uid": rfid_uid,
        }
    
    @app.post("/api/admin/assign-locker")
    def admin_assign_locker():
        """Admin endpoint to assign a locker to a guest."""
        data = request.get_json(silent=True) or {}
        guest_id = data.get("guest_id")
        locker_id = data.get("locker_id")
        notes = (data.get("notes") or "").strip()
        admin_id = data.get("admin_id") or session.get("admin_id")
        
        try:
            guest_id = int(guest_id)
            locker_id = int(locker_id)
        except (TypeError, ValueError):
            return {"error": "invalid guest_id or locker_id"}, 400
        
        with connect() as conn:
            # Verify guest exists and is active
            guest = conn.execute(
                "SELECT id, full_name, locker_id FROM members WHERE id = ? AND member_type = 'guest'",
                (guest_id,),
            ).fetchone()
            
            if not guest:
                return {"error": "guest_not_found"}, 404
            
            # Check if guest already has a locker assigned
            if guest["locker_id"]:
                return {"error": "guest_already_has_locker", "current_locker": guest["locker_id"]}, 409

            # Enforce guest-only locker assignments.
            if locker_id not in GUEST_LOCKER_IDS:
                return {"error": "locker_not_guest_accessible", "locker_id": locker_id}, 403
            
            # Check if locker is available (not assigned to anyone)
            existing_assignment = conn.execute(
                "SELECT id, full_name FROM members WHERE locker_id = ?",
                (locker_id,),
            ).fetchone()
            
            if existing_assignment:
                return {"error": "locker_already_assigned", "assigned_to": existing_assignment["full_name"]}, 409
            
            # Assign the locker to the guest
            conn.execute(
                "UPDATE members SET locker_id = ? WHERE id = ?",
                (locker_id, guest_id),
            )
            
            # Update locker status to occupied
            conn.execute(
                "UPDATE lockers SET status = 'occupied' WHERE id = ?",
                (locker_id,),
            )
            
            # Log the assignment
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("admin", str(admin_id or "unknown"), "locker_assigned", f"guest_id={guest_id}; locker_id={locker_id}; notes={notes}"),
            )
            
            conn.commit()
        
        return {
            "status": "ok",
            "message": f"Locker {locker_id} assigned to {guest['full_name']}",
            "guest_id": guest_id,
            "locker_id": locker_id
        }
    
    @app.post("/api/admin/card-mark-lost")
    def admin_card_mark_lost():
        """Admin marks a guest RFID card as lost and blacklists it."""
        if not request.is_json:
            return json_error("invalid_request", "Request body must be JSON.", 400)

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return json_error("invalid_request", "Request body must be a JSON object.", 400)

        rfid_uid = (data.get("rfid_uid") or "").strip()
        notes = (data.get("notes") or "").strip()
        charge_fee = data.get("charge_fee", False)  # Optional: charge replacement fee

        if not rfid_uid:
            return json_error("missing_rfid_uid", "The rfid_uid field is required.", 400)

        try:
            with connect() as conn:
                # Get the latest card entry for this RFID first.
                card = conn.execute(
                    "SELECT id, guest_id, status FROM guest_rfid_cards WHERE rfid_uid = ? ORDER BY issue_time DESC, id DESC LIMIT 1",
                    (rfid_uid,),
                ).fetchone()
                legacy_guest = None
                if not card:
                    # Fallback for legacy guest entries stored only in members
                    legacy_guest = conn.execute(
                        "SELECT id AS guest_id, expiry_date FROM members WHERE member_type = 'guest' AND rfid_uid = ?",
                        (rfid_uid,),
                    ).fetchone()
                    if not legacy_guest:
                        return json_error("card_not_found", "RFID card not found.", 404)
                guest_id = (card or legacy_guest)["guest_id"]

                # Update card status to LOST and BLACKLIST it
                locker_row = conn.execute(
                    "SELECT locker_id FROM members WHERE id = ?",
                    (guest_id,),
                ).fetchone()
                locker_id = locker_row["locker_id"] if locker_row and locker_row["locker_id"] else None

                if card:
                    conn.execute(
                        """UPDATE guest_rfid_cards 
                           SET status = 'BLACKLISTED', return_notes = ?, locker_id = ?
                           WHERE rfid_uid = ?""",
                        (
                            f"LOST - {notes}" if notes else "LOST - Card marked by admin",
                            locker_id,
                            rfid_uid,
                        ),
                    )
                else:
                    from datetime import datetime
                    issue_time = datetime.now().isoformat()
                    expires_at = ""
                    expected_return = None
                    conn.execute(
                        "INSERT INTO guest_rfid_cards (guest_id, rfid_uid, status, issue_time, expires_at, expected_return_time, return_notes, locker_id) VALUES (?, ?, 'BLACKLISTED', ?, ?, ?, ?, ?)",
                        (
                            guest_id,
                            rfid_uid,
                            issue_time,
                            expires_at,
                            expected_return,
                            f"LOST - {notes}" if notes else "LOST - Card marked by admin",
                            locker_id,
                        ),
                    )

                # Log the action
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("admin", "system", "card_blacklisted", f"rfid_uid={rfid_uid}; reason=lost"),
                )

                # Optional: charge replacement fee
                if charge_fee:
                    add_card_fee_to_payment(guest_id, f"Lost RFID card replacement - {rfid_uid}")

            return json_success({"status": "ok", "message": f"Card {rfid_uid} blacklisted", "fee_charged": charge_fee})
        except Exception:
            import traceback
            traceback.print_exc()
            return json_error("server_error", "Unable to blacklist card due to internal error.", 500)
    
    @app.post("/api/admin/card-mark-returned")
    def admin_card_mark_returned():
        """Admin marks a guest RFID card as returned."""
        if not request.is_json:
            return json_error("invalid_request", "Request body must be JSON.", 400)

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return json_error("invalid_request", "Request body must be a JSON object.", 400)

        rfid_uid = (data.get("rfid_uid") or "").strip()
        notes = (data.get("notes") or "").strip()
        admin_id = data.get("admin_id") or session.get("admin_id")

        if not rfid_uid:
            return json_error("missing_rfid_uid", "The rfid_uid field is required.", 400)

        from datetime import datetime

        try:
            with connect() as conn:
                card = conn.execute(
                    "SELECT id, guest_id, status FROM guest_rfid_cards WHERE rfid_uid = ? ORDER BY issue_time DESC, id DESC LIMIT 1",
                    (rfid_uid,),
                ).fetchone()

                legacy_guest = None
                if not card:
                    legacy_guest = conn.execute(
                        "SELECT id AS guest_id FROM members WHERE member_type = 'guest' AND rfid_uid = ?",
                        (rfid_uid,),
                    ).fetchone()
                    if not legacy_guest:
                        return json_error(
                            "card_not_found",
                            f"RFID card {rfid_uid} was not found or is not registered in the system.",
                            404,
                        )

                guest_id = card["guest_id"] if card else legacy_guest["guest_id"]

                if card and card["status"] == "RETURNED":
                    return json_success({
                        "status": "ok",
                        "message": f"Card {rfid_uid} is already marked as returned.",
                    })

                if card and card["status"] == "BLACKLISTED":
                    return json_error(
                        "card_blacklisted",
                        "This card has been blacklisted and cannot be returned.",
                        409,
                    )

                locker_row = conn.execute(
                    "SELECT locker_id FROM members WHERE id = ?",
                    (guest_id,),
                ).fetchone()
                locker_id = locker_row["locker_id"] if locker_row and locker_row["locker_id"] else None

                if getattr(conn, "_is_postgres", False):
                    schema_rows = conn.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                        ("guest_rfid_cards",),
                    ).fetchall()
                    existing_columns = {row["column_name"] for row in schema_rows}
                else:
                    schema_rows = conn.execute("PRAGMA table_info(guest_rfid_cards)").fetchall()
                    existing_columns = {row[1] for row in schema_rows}

                supports_return_admin = "return_admin_id" in existing_columns
                supports_actual_return = "actual_return_time" in existing_columns
                supports_return_notes = "return_notes" in existing_columns
                supports_locker_id = "locker_id" in existing_columns

                if supports_return_admin and admin_id is not None:
                    try:
                        admin_id = int(admin_id)
                    except (ValueError, TypeError):
                        admin_id = None
                    if admin_id is not None:
                        valid_admin = conn.execute(
                            "SELECT id FROM members WHERE id = ?",
                            (admin_id,),
                        ).fetchone()
                        if not valid_admin:
                            admin_id = None

                if card:
                    update_fields = ["status = 'RETURNED'"]
                    params = []
                    if supports_return_admin and admin_id is not None:
                        update_fields.append("return_admin_id = ?")
                        params.append(admin_id)
                    if supports_actual_return:
                        update_fields.append("actual_return_time = ?")
                        params.append(datetime.now().isoformat())
                    if supports_return_notes:
                        update_fields.append("return_notes = ?")
                        params.append(notes or "Returned to admin")
                    if supports_locker_id:
                        update_fields.append("locker_id = ?")
                        params.append(locker_id)

                    params.append(card["id"])
                    cursor = conn.execute(
                        f"UPDATE guest_rfid_cards SET {', '.join(update_fields)} WHERE id = ?",
                        tuple(params),
                    )
                else:
                    issue_time = datetime.now().isoformat()
                    expires_at = ""
                    expected_return = None
                    insert_columns = [
                        "guest_id",
                        "rfid_uid",
                        "status",
                        "issue_time",
                        "expires_at",
                        "expected_return_time",
                    ]
                    insert_values = [guest_id, rfid_uid, "RETURNED", issue_time, expires_at, expected_return]
                    if supports_actual_return:
                        insert_columns.append("actual_return_time")
                        insert_values.append(datetime.now().isoformat())
                    if supports_return_admin:
                        insert_columns.append("return_admin_id")
                        insert_values.append(admin_id)
                    if supports_return_notes:
                        insert_columns.append("return_notes")
                        insert_values.append(notes or "Returned to admin")
                    if supports_locker_id:
                        insert_columns.append("locker_id")
                        insert_values.append(locker_id)

                    column_list = ", ".join(insert_columns)
                    placeholder_list = ", ".join("?" for _ in insert_columns)
                    cursor = conn.execute(
                        f"INSERT INTO guest_rfid_cards ({column_list}) VALUES ({placeholder_list})",
                        tuple(insert_values),
                    )

                if cursor.rowcount == 0:
                    return json_error(
                        "card_not_found",
                        f"RFID card {rfid_uid} was not found or is not registered in the system.",
                        404,
                    )

                locker = conn.execute(
                    "SELECT locker_id FROM members WHERE id = ?",
                    (guest_id,),
                ).fetchone()
                log_detail = f"rfid_uid={rfid_uid}; guest_id={guest_id}"
                if locker and locker["locker_id"]:
                    locker_id = locker["locker_id"]
                    conn.execute(
                        "UPDATE members SET locker_id = NULL WHERE id = ?",
                        (guest_id,),
                    )
                    conn.execute(
                        "UPDATE lockers SET status = 'available' WHERE id = ?",
                        (locker_id,),
                    )
                    log_detail = f"rfid_uid={rfid_uid}; guest_id={guest_id}; locker_id={locker_id}"

                conn.execute(
                    "UPDATE members SET rfid_uid = NULL WHERE id = ?",
                    (guest_id,),
                )
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("admin", str(admin_id or "unknown"), "card_returned", log_detail),
                )

            return json_success({"status": "ok", "message": f"Card {rfid_uid} marked as returned."})
        except Exception:
            import traceback
            traceback.print_exc()
            return json_error("server_error", "Unable to mark card returned due to internal error.", 500)

    @app.get("/api/admin/guest-list")
    def admin_guest_list():
        """Return current active guest records for card issuance."""
        q = (request.args.get("q") or "").strip()
        now = datetime.now().isoformat()
        with connect() as conn:
            if q:
                guests = conn.execute(
                    """
                    SELECT id, full_name, locker_id, rfid_uid, expiry_date
                    FROM members
                    WHERE member_type = 'guest'
                      AND (full_name LIKE ? OR CAST(id AS TEXT) LIKE ? OR rfid_uid LIKE ?)
                    ORDER BY full_name ASC
                    LIMIT 200
                    """,
                    (f"%{q}%", f"%{q}%", f"%{q}%"),
                ).fetchall()
            else:
                guests = conn.execute(
                    """
                    SELECT id, full_name, locker_id, rfid_uid, expiry_date
                    FROM members
                    WHERE member_type = 'guest'
                    ORDER BY full_name ASC
                    LIMIT 200
                    """,
                ).fetchall()

        return {"guests": [dict(g) for g in guests]}

    def _load_card_audit_records(conn):
        from datetime import datetime

        now = datetime.now().isoformat()

        card_rows = conn.execute(
            """SELECT grc.*, m.full_name as guest_name, m.contact_number, 
                      COALESCE(grc.locker_id, m.locker_id) as locker_id
                   FROM guest_rfid_cards grc
                   LEFT JOIN members m ON grc.guest_id = m.id
                   ORDER BY grc.issue_time DESC""",
        ).fetchall()

        cards = [dict(card) for card in card_rows]
        existing_rfids = {card["rfid_uid"] for card in cards if card.get("rfid_uid")}

        legacy_rows = conn.execute(
            """SELECT id as guest_id, full_name as guest_name, contact_number, locker_id,
                      rfid_uid
                   FROM members
                   WHERE member_type = 'guest'
                     AND rfid_uid IS NOT NULL
                     AND rfid_uid NOT IN (SELECT rfid_uid FROM guest_rfid_cards)""",
        ).fetchall()

        for row in legacy_rows:
            card = dict(row)
            card["status"] = "ACTIVE"
            card["issue_time"] = None
            card["expires_at"] = ""
            card["expected_return_time"] = None
            card["actual_return_time"] = None
            card["checkout_notes"] = "Legacy guest RFID card"
            card["return_notes"] = None
            card["guest_id"] = card.get("guest_id")
            card["locker_id"] = card.get("locker_id")
            cards.append(card)

        for card in cards:
            status = card.get("status")
            if status == "EXPIRED":
                card["status"] = "RETURNED"
                status = "RETURNED"
            if status == "LOST":
                card["action_needed"] = "MISSING"
            else:
                card["action_needed"] = status

        return cards

    @app.post("/api/admin/force-lock-locker")
    def admin_force_lock_locker():
        """Admin endpoint to force-lock a locker (emergency recovery)."""
        data = request.get_json(silent=True) or {}
        locker_id = data.get("locker_id")
        reason = (data.get("reason") or "").strip()
        
        if not locker_id:
            return {"error": "missing locker_id"}, 400
        
        try:
            # Send force-lock command to ESP32
            get_device().lock(int(locker_id))
            
            with connect() as conn:
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("admin", "system", "force_locked_locker", f"locker_id={locker_id}; reason={reason or 'card_lost'}"),
                )
            
            return {"status": "ok", "message": f"Locker {locker_id} force-locked"}
        except RequestException as e:
            msg = f"ESP32 unreachable during force-lock: {str(e)}"
            print(f"[ADMIN] {msg}")
            return {"status": "error", "reason": "device_unreachable", "error": msg}, 503
        except Exception as e:
            msg = f"Failed to force-lock locker: {str(e)}"
            print(f"[ADMIN] {msg}")
            return {"status": "error", "error": msg}, 500

    @app.get("/api/admin/card-audit-report")
    def admin_card_audit_report():
        """Generate daily card audit report showing missing/overdue cards."""
        from datetime import datetime
        
        with connect() as conn:
            cards_list = _load_card_audit_records(conn)
            
            summary = {
                "active": sum(1 for c in cards_list if c.get("status") == "ACTIVE"),
                "returned": sum(1 for c in cards_list if c.get("status") == "RETURNED"),
                "lost": sum(1 for c in cards_list if c.get("status") == "LOST"),
                "blacklisted": sum(1 for c in cards_list if c.get("status") == "BLACKLISTED"),
                "returned_today": sum(
                    1 for c in cards_list
                    if c.get("actual_return_time")
                    and datetime.fromisoformat(c["actual_return_time"]).date() == datetime.now().date()
                ),
            }
            
            report = {
                "generated_at": datetime.now().isoformat(),
                "total_cards": len(cards_list),
                "cards": cards_list,
                "summary": summary,
            }
        
        return report

    @app.get("/api/admin/card-audit-history")
    def admin_card_audit_history():
        """Generate a full guest card audit history list."""
        q = (request.args.get("q") or "").strip()
        with connect() as conn:
            cards_list = _load_card_audit_records(conn)
            if q:
                q_lower = q.lower()
                cards_list = [
                    card for card in cards_list
                    if q_lower in (card.get("rfid_uid") or "").lower()
                    or q_lower in (card.get("guest_name") or "").lower()
                    or q_lower in str(card.get("locker_id") or "")
                    or q_lower in (card.get("status") or "").lower()
                    or q_lower in (card.get("checkout_notes") or "").lower()
                    or q_lower in (card.get("return_notes") or "").lower()
                ]
            history = {
                "generated_at": datetime.now().isoformat(),
                "total_records": len(cards_list),
                "cards": cards_list,
            }
        return history

    print("About to return app")
    # Start background jobs for card cleanup, email notifications, and payment processing
    start_background_jobs()
    return app


def main() -> None:
    app = create_app()
    # Listen on 0.0.0.0 to allow ESP32 hardware to connect from network
    # Disable the Flask reloader so shared enrollment state remains consistent.
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()

