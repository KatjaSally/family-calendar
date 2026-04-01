import os
import json
from datetime import datetime, timedelta
import calendar

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, session, send_from_directory, jsonify, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_PATH, override=True)

database_url = os.getenv("DATABASE_URL")

if not database_url:
    raise RuntimeError("DATABASE_URL was not found in the .env file.")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "family-calendar-secret")
db = SQLAlchemy(app)

JOIN_CODE = os.getenv("JOIN_CODE", "family123")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").replace("\\n", "\n")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:notifications@example.com")

USER_COLORS = [
    {"background": "#fce7f3", "border": "#f9a8d4"},
    {"background": "#dbeafe", "border": "#93c5fd"},
    {"background": "#dcfce7", "border": "#86efac"},
    {"background": "#fef3c7", "border": "#fcd34d"},
    {"background": "#ede9fe", "border": "#c4b5fd"},
    {"background": "#cffafe", "border": "#67e8f9"},
]


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False, default="")
    default_view = db.Column(db.String(20), nullable=False, default="today")
    theme = db.Column(db.String(20), nullable=False, default="light")


class AppointmentShare(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointment.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    note = db.Column(db.String(200), nullable=False, default="")

    user = db.relationship("User")


class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    appointment_time = db.Column(db.String(100), nullable=False)
    share_reason = db.Column(db.String(50), nullable=False, default="")
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    user = db.relationship("User", backref="appointments")
    shares = db.relationship(
        "AppointmentShare",
        backref="appointment",
        cascade="all, delete-orphan"
    )


class PushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    p256dh_key = db.Column(db.String(255), nullable=False)
    auth_key = db.Column(db.String(255), nullable=False)

    user = db.relationship("User", backref="push_subscriptions")


with app.app_context():
    db.create_all()
    db.session.execute(
        text("ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255) NOT NULL DEFAULT ''")
    )
    db.session.commit()


def parse_appointment_time(appointment):
    return datetime.strptime(appointment.appointment_time, "%Y-%m-%dT%H:%M")


def accepted_for_user(user_id):
    return Appointment.query.join(
        AppointmentShare
    ).filter(
        AppointmentShare.user_id == user_id,
        AppointmentShare.status == "accepted",
        Appointment.user_id != user_id
    ).all()


def pending_for_user(user_id):
    return Appointment.query.join(
        AppointmentShare
    ).filter(
        AppointmentShare.user_id == user_id,
        AppointmentShare.status == "pending",
        Appointment.user_id != user_id
    ).all()


def all_family_accepted_appointments():
    return Appointment.query.order_by(Appointment.appointment_time).all()


def build_today_timeline(appointments):
    timeline_items = []

    for appointment in appointments:
        dt = parse_appointment_time(appointment)
        minutes = dt.hour * 60 + dt.minute
        top_offset = int((minutes / 1440) * 1200)

        timeline_items.append({
            "appointment": appointment,
            "top": top_offset
        })

    return timeline_items


def build_week_grid(appointments, week_start):
    week_days = [week_start + timedelta(days=offset) for offset in range(7)]
    week_cells = {}

    for day in week_days:
        for hour in range(24):
            week_cells[(day.isoformat(), hour)] = []

    for appointment in appointments:
        dt = parse_appointment_time(appointment)
        day_key = dt.date().isoformat()
        hour_key = dt.hour

        if (day_key, hour_key) in week_cells:
            week_cells[(day_key, hour_key)].append(appointment)

    return week_days, week_cells


def build_month_grid(appointments, current_date):
    cal = calendar.Calendar(firstweekday=0)
    month_weeks = cal.monthdatescalendar(current_date.year, current_date.month)
    month_cells = {}

    for week in month_weeks:
        for day in week:
            month_cells[day.isoformat()] = []

    for appointment in appointments:
        day_key = parse_appointment_time(appointment).date().isoformat()
        if day_key in month_cells:
            month_cells[day_key].append(appointment)

    return month_weeks, month_cells


def build_user_color_map(users):
    color_map = {}

    for index, user in enumerate(users):
        color_map[user.id] = USER_COLORS[index % len(USER_COLORS)]

    return color_map


def appointment_to_form_parts(appointment):
    dt = parse_appointment_time(appointment)
    return {
        "iso_date": dt.strftime("%Y-%m-%d"),
        "day": f"{dt.day:02d}",
        "month": f"{dt.month:02d}",
        "year": str(dt.year),
        "hour": f"{dt.hour:02d}",
        "minute": f"{dt.minute:02d}",
    }


def build_appointment_time_from_form(form_data):
    appointment_date = form_data["appointment_date"]
    appointment_hour = form_data["appointment_hour"]
    appointment_minute = form_data["appointment_minute"]
    return f"{appointment_date}T{appointment_hour}:{appointment_minute}"


def parse_month_key(month_key, fallback_date):
    try:
        parsed = datetime.strptime(month_key, "%Y-%m")
        return parsed.date().replace(day=1)
    except (TypeError, ValueError):
        return fallback_date.replace(day=1)


def build_appointments_url(user_id, view, mode, month_key=None):
    params = {
        "user_id": user_id,
        "view": view,
        "mode": mode,
    }
    if view == "month" and month_key:
        params["month"] = month_key
    return url_for("appointments", **params)


def build_dashboard_url(user_id):
    return url_for("dashboard", user_id=user_id)


def build_invitation_url(user):
    return build_appointments_url(user.id, user.default_view, "my")


def build_form_choices():
    hours = [f"{hour:02d}" for hour in range(0, 24)]
    minutes = [f"{minute:02d}" for minute in range(0, 60, 5)]
    return hours, minutes


def logged_in_user_id():
    return session.get("user_id")


def require_profile_access(user_id):
    if logged_in_user_id() != user_id:
        return redirect(url_for("login", user_id=user_id))
    return None


def upcoming_appointments_for_user(user):
    now = datetime.now()
    appointments = Appointment.query.filter_by(user_id=user.id).all() + accepted_for_user(user.id)
    upcoming = [
        appointment for appointment in appointments
        if parse_appointment_time(appointment) >= now
    ]
    upcoming.sort(key=parse_appointment_time)
    return upcoming


def push_notifications_ready():
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_SUBJECT)


def send_push_notification(user, title, body, target_url):
    if not push_notifications_ready():
        return

    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return

    subscriptions = PushSubscription.query.filter_by(user_id=user.id).all()
    if not subscriptions:
        return

    payload = json.dumps(
        {
            "title": title,
            "body": body,
            "url": target_url,
        }
    )

    stale_subscription_ids = []

    for subscription in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {
                        "p256dh": subscription.p256dh_key,
                        "auth": subscription.auth_key,
                    },
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {404, 410}:
                stale_subscription_ids.append(subscription.id)

    if stale_subscription_ids:
        PushSubscription.query.filter(PushSubscription.id.in_(stale_subscription_ids)).delete(
            synchronize_session=False
        )
        db.session.commit()


@app.route("/")
def home():
    users = User.query.order_by(User.name).all()
    return render_template(
        "register.html",
        users=users,
        error_message="",
        status_message=request.args.get("status", ""),
        login_error="",
        selected_user=None
    )


@app.route("/create-user", methods=["POST"])
def create_user():
    name = request.form["name"].strip()
    join_code = request.form["join_code"].strip()
    password = request.form["password"]
    confirm_password = request.form["confirm_password"]

    users = User.query.order_by(User.name).all()

    if join_code != JOIN_CODE:
        return render_template(
            "register.html",
            users=users,
            error_message="That join code is not correct.",
            status_message="",
            login_error="",
            selected_user=None
        )

    if password != confirm_password:
        return render_template(
            "register.html",
            users=users,
            error_message="Passwords do not match.",
            status_message="",
            login_error="",
            selected_user=None
        )

    if name and password:
        new_user = User(
            name=name,
            password_hash=generate_password_hash(password),
            default_view="today",
            theme="light"
        )
        db.session.add(new_user)
        db.session.commit()
        session["user_id"] = new_user.id
        return redirect(build_dashboard_url(new_user.id))

    return redirect(url_for("home"))


@app.route("/login/<int:user_id>", methods=["GET", "POST"])
def login(user_id):
    user = User.query.get_or_404(user_id)
    users = User.query.order_by(User.name).all()

    if request.method == "POST":
        password = request.form["password"]
        if check_password_hash(user.password_hash, password):
            session["user_id"] = user.id
            return redirect(build_dashboard_url(user.id))

        return render_template(
            "register.html",
            users=users,
            error_message="",
            status_message="",
            login_error="That password is not correct.",
            selected_user=user
        )

    return render_template(
        "register.html",
        users=users,
        error_message="",
        status_message="",
        login_error="",
        selected_user=user
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home", status="You were logged out."))


@app.route("/dashboard/<int:user_id>")
def dashboard(user_id):
    access_redirect = require_profile_access(user_id)
    if access_redirect:
        return access_redirect

    user = User.query.get_or_404(user_id)
    pending_appointments = pending_for_user(user.id)
    pending_appointments.sort(key=parse_appointment_time)
    next_appointments = upcoming_appointments_for_user(user)[:3]

    return render_template(
        "dashboard.html",
        user=user,
        pending_appointments=pending_appointments,
        next_appointments=next_appointments,
        status_message=request.args.get("status", ""),
        push_ready=push_notifications_ready(),
        vapid_public_key=VAPID_PUBLIC_KEY
    )


@app.route("/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    access_redirect = require_profile_access(user_id)
    if access_redirect:
        return access_redirect

    user = User.query.get_or_404(user_id)
    user_name = user.name

    PushSubscription.query.filter_by(user_id=user.id).delete()
    AppointmentShare.query.filter_by(user_id=user.id).delete()

    owned_appointments = Appointment.query.filter_by(user_id=user.id).all()
    for appointment in owned_appointments:
        db.session.delete(appointment)

    db.session.delete(user)
    db.session.commit()

    return redirect(url_for("home", status=f'Profile "{user_name}" was deleted.'))


@app.route("/users/<int:user_id>/preferences", methods=["POST"])
def save_preferences(user_id):
    access_redirect = require_profile_access(user_id)
    if access_redirect:
        return access_redirect

    user = User.query.get_or_404(user_id)

    default_view = request.form.get("default_view", "today")
    theme = request.form.get("theme", "light")

    if default_view not in ["today", "week", "month"]:
        default_view = "today"

    if theme not in ["light", "dark"]:
        theme = "light"

    user.default_view = default_view
    user.theme = theme
    db.session.commit()

    return redirect(build_dashboard_url(user.id))


@app.route("/users/<int:user_id>/rename", methods=["POST"])
def rename_user(user_id):
    access_redirect = require_profile_access(user_id)
    if access_redirect:
        return access_redirect

    user = User.query.get_or_404(user_id)
    new_name = request.form["name"].strip()

    if new_name:
        user.name = new_name
        db.session.commit()

    return redirect(build_dashboard_url(user.id))


@app.route("/users/<int:user_id>/change-password", methods=["POST"])
def change_password(user_id):
    access_redirect = require_profile_access(user_id)
    if access_redirect:
        return access_redirect

    user = User.query.get_or_404(user_id)
    current_password = request.form["current_password"]
    new_password = request.form["new_password"]
    confirm_password = request.form["confirm_password"]

    if not check_password_hash(user.password_hash, current_password):
        return redirect(url_for("dashboard", user_id=user.id, status="Current password is not correct."))

    if new_password != confirm_password:
        return redirect(url_for("dashboard", user_id=user.id, status="New passwords do not match."))

    if not new_password:
        return redirect(url_for("dashboard", user_id=user.id, status="New password cannot be empty."))

    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    return redirect(url_for("dashboard", user_id=user.id, status="Password changed."))


@app.route("/appointments/<int:user_id>/new", methods=["GET", "POST"])
def add_appointment(user_id):
    access_redirect = require_profile_access(user_id)
    if access_redirect:
        return access_redirect

    user = User.query.get_or_404(user_id)

    if request.method == "POST":
        title = request.form["title"]
        appointment_time = build_appointment_time_from_form(request.form)

        share_reason = request.form.get("share_reason", "")
        selected_user_ids = request.form.getlist("shared_with")

        new_appointment = Appointment(
            title=title,
            appointment_time=appointment_time,
            share_reason=share_reason if selected_user_ids else "",
            user_id=user.id
        )

        db.session.add(new_appointment)
        db.session.flush()

        recipient_users = []

        for selected_user_id in selected_user_ids:
            selected_user = User.query.get(int(selected_user_id))
            if selected_user:
                recipient_users.append(selected_user)
            share = AppointmentShare(
                appointment_id=new_appointment.id,
                user_id=int(selected_user_id),
                status="pending",
                note=""
            )
            db.session.add(share)

        db.session.commit()

        for recipient_user in recipient_users:
            send_push_notification(
                recipient_user,
                f"{user.name} tagged you",
                f'{user.name} shared "{new_appointment.title}" with you.',
                build_invitation_url(recipient_user),
            )

        return redirect(build_appointments_url(user.id, user.default_view, "my"))

    other_users = User.query.filter(User.id != user.id).order_by(User.name).all()
    hours, minutes = build_form_choices()

    return render_template(
        "add_appointment.html",
        user=user,
        other_users=other_users,
        hours=hours,
        minutes=minutes
    )


@app.route("/appointments/<int:user_id>", methods=["GET", "POST"])
def appointments(user_id):
    access_redirect = require_profile_access(user_id)
    if access_redirect:
        return access_redirect

    user = User.query.get_or_404(user_id)

    selected_view = request.args.get("view", user.default_view)
    if selected_view not in ["today", "week", "month"]:
        selected_view = user.default_view

    selected_mode = request.args.get("mode", "my")
    if selected_mode not in ["my", "family"]:
        selected_mode = "my"

    now = datetime.now()
    today_date = now.date()
    week_start = today_date - timedelta(days=today_date.weekday())
    week_end = week_start + timedelta(days=6)
    selected_month_date = parse_month_key(request.args.get("month"), today_date)
    month_start = selected_month_date.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    previous_month = (month_start - timedelta(days=1)).replace(day=1)
    month_end = next_month - timedelta(days=1)

    own_appointments = Appointment.query.filter_by(user_id=user.id).all()
    accepted_shared_appointments = accepted_for_user(user.id)
    pending_appointments = pending_for_user(user.id)

    if selected_mode == "my":
        base_appointments = own_appointments + accepted_shared_appointments
    else:
        base_appointments = all_family_accepted_appointments()

    base_appointments.sort(key=parse_appointment_time)

    if selected_view == "today":
        calendar_appointments = [
            appointment for appointment in base_appointments
            if parse_appointment_time(appointment).date() == today_date
        ]
    elif selected_view == "week":
        calendar_appointments = [
            appointment for appointment in base_appointments
            if week_start <= parse_appointment_time(appointment).date() <= week_end
        ]
    else:
        calendar_appointments = [
            appointment for appointment in base_appointments
            if month_start <= parse_appointment_time(appointment).date() <= month_end
        ]

    pending_appointments.sort(key=parse_appointment_time)
    timeline_appointments = build_today_timeline(calendar_appointments) if selected_view == "today" else []

    week_days = []
    week_cells = {}
    if selected_view == "week":
        week_days, week_cells = build_week_grid(calendar_appointments, week_start)

    month_weeks = []
    month_cells = {}
    if selected_view == "month":
        month_weeks, month_cells = build_month_grid(calendar_appointments, today_date)

    all_users = User.query.order_by(User.name).all()
    other_users = User.query.filter(User.id != user.id).order_by(User.name).all()
    user_color_map = build_user_color_map(all_users)
    hour_labels = [f"{hour:02d}:00" for hour in range(24)]

    hours, minutes = build_form_choices()
    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    editable_appointments = Appointment.query.filter_by(user_id=user.id).order_by(Appointment.appointment_time).all()
    editable_data = {
        appointment.id: appointment_to_form_parts(appointment)
        for appointment in editable_appointments
    }

    return render_template(
        "appointments.html",
        user=user,
        users=all_users,
        other_users=other_users,
        calendar_appointments=calendar_appointments,
        pending_appointments=pending_appointments,
        selected_view=selected_view,
        selected_mode=selected_mode,
        timeline_appointments=timeline_appointments,
        hour_labels=hour_labels,
        hours=hours,
        minutes=minutes,
        week_days=week_days,
        week_cells=week_cells,
        month_weeks=month_weeks,
        month_cells=month_cells,
        weekday_names=weekday_names,
        current_month_label=month_start.strftime("%B %Y"),
        current_month_key=month_start.strftime("%Y-%m"),
        previous_month_key=previous_month.strftime("%Y-%m"),
        next_month_key=next_month.strftime("%Y-%m"),
        user_color_map=user_color_map,
        editable_appointments=editable_appointments,
        editable_data=editable_data
    )


@app.route("/appointments/<int:user_id>/edit/<int:appointment_id>", methods=["POST"])
def edit_appointment(user_id, appointment_id):
    access_redirect = require_profile_access(user_id)
    if access_redirect:
        return access_redirect

    user = User.query.get_or_404(user_id)
    appointment = Appointment.query.get_or_404(appointment_id)

    if appointment.user_id != user.id:
        selected_view = request.args.get("view", user.default_view)
        selected_mode = request.args.get("mode", "my")
        month_key = request.args.get("month")
        return redirect(build_appointments_url(user.id, selected_view, selected_mode, month_key))

    appointment.title = request.form["title"]
    appointment.appointment_time = build_appointment_time_from_form(request.form)

    selected_user_ids = {int(user_id_value) for user_id_value in request.form.getlist("shared_with")}
    appointment.share_reason = request.form.get("share_reason", "") if selected_user_ids else ""

    current_shares = {share.user_id: share for share in appointment.shares}

    for share_user_id, share in list(current_shares.items()):
        if share_user_id not in selected_user_ids:
            db.session.delete(share)

    for selected_user_id in selected_user_ids:
        if selected_user_id not in current_shares:
            db.session.add(
                AppointmentShare(
                    appointment_id=appointment.id,
                    user_id=selected_user_id,
                    status="pending",
                    note=""
                )
            )

    db.session.commit()

    selected_view = request.args.get("view", user.default_view)
    selected_mode = request.args.get("mode", "my")
    month_key = request.args.get("month")
    return redirect(build_appointments_url(user.id, selected_view, selected_mode, month_key))


@app.route("/appointments/<int:user_id>/delete/<int:appointment_id>", methods=["POST"])
def delete_appointment(user_id, appointment_id):
    access_redirect = require_profile_access(user_id)
    if access_redirect:
        return access_redirect

    user = User.query.get_or_404(user_id)
    appointment = Appointment.query.get_or_404(appointment_id)

    if appointment.user_id == user.id and request.form.get("confirm_delete") == "yes":
        db.session.delete(appointment)
        db.session.commit()

    selected_view = request.args.get("view", user.default_view)
    selected_mode = request.args.get("mode", "my")
    month_key = request.args.get("month")
    return redirect(build_appointments_url(user.id, selected_view, selected_mode, month_key))


@app.route("/appointments/<int:user_id>/accept/<int:appointment_id>", methods=["POST"])
def accept_appointment(user_id, appointment_id):
    access_redirect = require_profile_access(user_id)
    if access_redirect:
        return access_redirect

    user = User.query.get_or_404(user_id)

    share = AppointmentShare.query.filter_by(
        appointment_id=appointment_id,
        user_id=user_id
    ).first_or_404()

    share.status = "accepted"
    share.note = ""
    db.session.commit()

    selected_view = request.args.get("view", user.default_view)
    selected_mode = request.args.get("mode", "my")
    month_key = request.args.get("month")
    return redirect(build_appointments_url(user_id, selected_view, selected_mode, month_key))


@app.route("/appointments/<int:user_id>/decline/<int:appointment_id>", methods=["POST"])
def decline_appointment(user_id, appointment_id):
    access_redirect = require_profile_access(user_id)
    if access_redirect:
        return access_redirect

    user = User.query.get_or_404(user_id)

    share = AppointmentShare.query.filter_by(
        appointment_id=appointment_id,
        user_id=user_id
    ).first_or_404()

    share.status = "declined"
    share.note = request.form.get("decline_note", "").strip()
    db.session.commit()

    selected_view = request.args.get("view", user.default_view)
    selected_mode = request.args.get("mode", "my")
    month_key = request.args.get("month")
    return redirect(build_appointments_url(user_id, selected_view, selected_mode, month_key))


@app.route("/push/subscribe", methods=["POST"])
def save_push_subscription():
    user_id = logged_in_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in."}), 401

    if not push_notifications_ready():
        return jsonify({"error": "Push notifications are not configured yet."}), 503

    payload = request.get_json(silent=True) or {}
    endpoint = payload.get("endpoint", "")
    keys = payload.get("keys", {})
    p256dh_key = keys.get("p256dh", "")
    auth_key = keys.get("auth", "")

    if not endpoint or not p256dh_key or not auth_key:
        return jsonify({"error": "Subscription data is incomplete."}), 400

    existing_subscription = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if existing_subscription:
        existing_subscription.user_id = user_id
        existing_subscription.p256dh_key = p256dh_key
        existing_subscription.auth_key = auth_key
    else:
        db.session.add(
            PushSubscription(
                user_id=user_id,
                endpoint=endpoint,
                p256dh_key=p256dh_key,
                auth_key=auth_key,
            )
        )

    db.session.commit()
    return jsonify({"ok": True})


@app.route("/push/unsubscribe", methods=["POST"])
def remove_push_subscription():
    user_id = logged_in_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in."}), 401

    payload = request.get_json(silent=True) or {}
    endpoint = payload.get("endpoint", "")
    if not endpoint:
        return jsonify({"error": "Endpoint missing."}), 400

    PushSubscription.query.filter_by(user_id=user_id, endpoint=endpoint).delete()
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/service-worker.js")
def service_worker():
    return send_from_directory(app.static_folder, "service-worker.js", mimetype="application/javascript")


if __name__ == "__main__":
    app.run(debug=True)
