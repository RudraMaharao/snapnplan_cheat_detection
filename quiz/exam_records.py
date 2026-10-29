"""Exam lifecycle and CSV serialization. CSVs are generated in memory only."""
import csv
import io
import json
import math
from contextlib import contextmanager
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils import timezone

from .models import ExamSession

FLAGGED = {"other_window_visible", "left_exam_tab", "sharing_stopped"}
FINISH_LABELS = {
    "submitted": "Submitted",
    "timeout": "Time expired — latest saved answers",
    "ended": "Ended by invigilator — latest saved answers",
}


@contextmanager
def locked_exam(code):
    # Take the write lock before reading. select_for_update alone is a no-op
    # on SQLite, the production database. End, autosave, submit, join and
    # delete must serialize so a late request cannot change frozen results.
    with transaction.atomic():
        ExamSession.objects.filter(code=code).update(mode=F("mode"))
        yield get_object_or_404(ExamSession, code=code)


def deadline(attempt):
    if attempt.began_at is None:
        return None
    return attempt.began_at + timedelta(minutes=attempt.session.minutes)


def finish(attempt, at, reason):
    if attempt.submitted_at is not None:
        return
    attempt.submitted_at = at
    attempt.finish_reason = reason
    attempt.state = "stopped"
    attempt.save(update_fields=["submitted_at", "finish_reason", "state"])


def expire(attempt, now=None):
    due = deadline(attempt)
    if due and (now or timezone.now()) >= due:
        finish(attempt, due, "timeout")


def clean_snapshot(payload, session):
    """Reject malformed snapshots before any result or log can be overwritten."""
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object.")
    answers = payload.get("answers", [])
    report = payload.get("report", {})
    if not isinstance(answers, list) or len(answers) > len(session.questions):
        raise ValueError("Invalid answers.")
    answers = answers + [None] * (len(session.questions) - len(answers))
    for answer, question in zip(answers, session.questions):
        if answer is not None and (type(answer) is not int or
                not 0 <= answer < len(question.get("options", []))):
            raise ValueError("Invalid answer option.")
    if not isinstance(report, dict):
        raise ValueError("Invalid monitoring report.")
    events, stats = report.get("events", []), report.get("stats", {})
    if not isinstance(events, list) or len(events) > 20000 or not isinstance(stats, dict):
        raise ValueError("Invalid event log.")
    cleaned = []
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("Invalid event.")
        item = {}
        for field in ("type", "mode", "at", "note"):
            value = event.get(field, "")
            if not isinstance(value, str):
                raise ValueError("Invalid event text.")
            item[field] = value[:200]
        for field in ("seconds", "elapsed"):
            value = event.get(field, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 86400:
                raise ValueError("Invalid event duration.")
            item[field] = round(value, 2)
        cleaned.append(item)
    safe_stats = {}
    for key in ("duration_s", "monitored_s", "permitted_s", "paused_s",
                "frames_checked", "frames_detected", "confirmed_events",
                "flagged_seconds", "tab_aways", "tab_away_seconds"):
        value = stats.get(key, 0)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10000000:
            raise ValueError("Invalid monitoring statistic.")
        safe_stats[key] = round(value, 2)
    return answers, cleaned, safe_stats


def save_snapshot(attempt, payload, cleaned, now):
    revision = payload.get("revision", attempt.revision + 1)
    if type(revision) is not int or not 1 <= revision <= 2147483647:
        raise ValueError("Invalid save revision.")
    if revision <= attempt.revision:
        return False
    attempt.answers, attempt.events, attempt.stats = cleaned
    attempt.score = sum(answer is not None and answer == q.get("answer_index")
                        for answer, q in zip(attempt.answers, attempt.session.questions))
    attempt.flag_count = sum(e["type"] in FLAGGED for e in attempt.events)
    attempt.revision = revision
    attempt.saved_at = attempt.last_seen = now
    state = payload.get("state", "")
    if state in {"ok", "warning", "flagged", "permitted", "paused", "stopped"}:
        attempt.state = state
    attempt.save()
    return True


def attempt_summary(attempt, now=None):
    total = len(attempt.session.questions)
    answered = sum(answer is not None for answer in attempt.answers[:total])
    end = attempt.submitted_at or now or timezone.now()
    # Old attempts predate began_at. Label the joined-time estimate instead
    # of pretending it measured their actual screen-sharing time.
    start = attempt.began_at
    legacy = start is None and attempt.submitted_at is not None and not attempt.finish_reason
    if legacy:
        start = attempt.started_at
    seconds = max(0, int((end - start).total_seconds())) if start else None
    status = FINISH_LABELS.get(attempt.finish_reason,
                              "Submitted" if attempt.submitted_at else "In progress")
    if start is None:
        status = "Ended before starting" if attempt.submitted_at else "Not started"
    counts = {kind: sum(e.get("type") == kind for e in attempt.events)
              for kind in FLAGGED}
    return {
        "attempt": attempt, "status": status, "total": total,
        "answered": answered, "unanswered": total - answered,
        "wrong": max(0, answered - attempt.score),
        "percentage": round(attempt.score * 100 / total, 2) if total else 0,
        "seconds": seconds, "time_taken": (f"{seconds // 60}:{seconds % 60:02d}" if seconds is not None else "—"),
        "time_basis": "joined time (legacy estimate)" if legacy else "screen sharing" if start else "not started",
        "window_flags": counts["other_window_visible"],
        "tab_flags": counts["left_exam_tab"],
        "sharing_stops": counts["sharing_stopped"],
    }


def _iso(value):
    return value.isoformat() if value else ""


def _safe_cell(value):
    # csv quoting alone does not prevent Excel/Sheets formula execution.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + value
    return value


def results_csv(session, attempts):
    out = io.StringIO(newline="")
    out.write("\ufeff")  # UTF-8 BOM for names opened directly in Excel.
    writer = csv.writer(out)
    writer.writerow([
        "Exam code", "Exam title", "Exam created (UTC)", "Exam ended (UTC)",
        "Allowed minutes", "Student attempt ID", "Student name", "Student ID / roll number",
        "Status", "Marks", "Maximum marks", "Percentage", "Answered", "Wrong", "Unanswered",
        "Joined (UTC)", "Screen sharing started (UTC)", "Finished (UTC)",
        "Time taken (seconds)", "Time taken (minutes:seconds)", "Time basis",
        "Last saved (UTC)", "Last seen (UTC)", "Flags", "Window flags", "Tab switches",
        "Sharing stops", "Flagged window seconds", "Permitted window seconds",
        "Monitored seconds", "Permitted seconds", "Paused seconds", "Frames checked",
        "Tab away seconds", "Answers (JSON, zero-based option indexes)", "Events (JSON)",
        "Monitoring statistics (JSON)",
    ])
    for attempt in attempts:
        row = attempt_summary(attempt)
        writer.writerow([_safe_cell(value) for value in [
            session.code, session.title, _iso(session.created_at), _iso(session.ended_at),
            session.minutes, attempt.id, attempt.name, attempt.student_id,
            row["status"], attempt.score, row["total"], row["percentage"], row["answered"], row["wrong"], row["unanswered"],
            _iso(attempt.started_at), _iso(attempt.began_at), _iso(attempt.submitted_at),
            row["seconds"] if row["seconds"] is not None else "", row["time_taken"], row["time_basis"],
            _iso(attempt.saved_at), _iso(attempt.last_seen), attempt.flag_count, row["window_flags"], row["tab_flags"],
            row["sharing_stops"], attempt.flagged_seconds, attempt.permitted_seconds,
            attempt.stats.get("monitored_s", 0), attempt.stats.get("permitted_s", 0),
            attempt.stats.get("paused_s", 0), attempt.stats.get("frames_checked", 0),
            attempt.stats.get("tab_away_seconds", 0),
            json.dumps(attempt.answers, ensure_ascii=False),
            json.dumps(attempt.events, ensure_ascii=False),
            json.dumps(attempt.stats, ensure_ascii=False),
        ]])
    return out.getvalue()


def events_csv(session, attempts):
    out = io.StringIO(newline="")
    out.write("\ufeff")
    writer = csv.writer(out)
    writer.writerow(["Exam code", "Exam title", "Student attempt ID", "Student name", "Student ID / roll number",
                     "Event type", "At (UTC, reported by browser)", "Elapsed seconds", "Duration seconds", "Mode", "Note"])
    for attempt in attempts:
        for event in attempt.events:
            writer.writerow([_safe_cell(value) for value in [session.code, session.title, attempt.id,
                attempt.name, attempt.student_id, event.get("type", ""), event.get("at", ""),
                event.get("elapsed", ""), event.get("seconds", ""), event.get("mode", ""), event.get("note", "")]])
    return out.getvalue()
