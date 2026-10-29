"""Exam mode: server side.

Drop these into quiz/views.py (and the urls at the bottom into quiz/urls.py).

The invigilator sets one mode for a session; every student's browser polls it
every few seconds and switches. Polling rather than WebSockets on purpose:
PythonAnywhere's free tier has no persistent sockets, and a 5 second poll is
more than fast enough for "everyone may look things up now".

An ExamSession is identified by a short code the invigilator reads out, so no
accounts are needed for a classroom demo.
"""
import json
import os
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from django.urls import reverse

from .models import ExamSession, ExamAttempt   # see models.py below
from .exam_records import (locked_exam, deadline, finish, expire, clean_snapshot,
                           save_snapshot, attempt_summary, results_csv, events_csv)


MODES = {"monitoring", "permitted", "paused"}


# Prefills the question box on the create page, so the format is obvious from
# an example rather than from instructions nobody reads. The invigilator
# replaces it with their own paper.
#
# If you ever want questions generated instead of typed, exam_create just
# needs a list of {question, options, answer_index} - swap parse_questions()
# for a call to make_quiz() and nothing downstream changes.
SAMPLE_TEXT = """\
Which data structure gives O(1) average lookup by key?
- Array
* Hash table
- Linked list
- Binary search tree

What is the worst-case time complexity of quicksort?
- O(n)
- O(n log n)
* O(n^2)
- O(log n)

Which traversal of a binary search tree yields sorted order?
- Pre-order
* In-order
- Post-order
- Level-order
"""


# ------------------------------------------------------------- the gate ---
#
# WHAT THIS IS, AND WHAT IT IS NOT
#
# A shared passcode that stops a student wandering into /exam/new/ and
# starting sessions. It is NOT authentication: everyone who knows it is the
# same person as far as the app is concerned, it cannot be revoked for one
# individual, and a determined student who watches an invigilator type it has
# it forever.
#
# The production answer is Django's own auth with a staff check:
#
#     from django.contrib.auth.decorators import user_passes_test
#     @user_passes_test(lambda u: u.is_staff)
#     def exam_create(request): ...
#
# That gives real accounts, per-person revocation and an audit trail of who
# started which exam. Say so plainly in the write-up rather than implying this
# gate is more than it is.
#
# Note what was ALREADY protected before this gate existed: exam_dashboard and
# exam_set_mode compare owner_key against the browser session, so knowing a
# session code never let a student open the dashboard or pause the room. The
# only gap was who may CREATE a session.

MAX_TRIES = 5


def _configured_passcode():
    """settings.EXAM_PASSCODE, else the EXAM_PASSCODE env var, else None.

    Returning None fails CLOSED - exam mode refuses to open rather than
    falling back to a default passcode. A default would be worse than no gate
    at all, because it would look protected while being public.
    """
    code = getattr(settings, "EXAM_PASSCODE", None) or os.environ.get(
        "EXAM_PASSCODE", "")
    return code.strip() or None


def _history_access(request):
    return request.session.get("exam_history_until", 0) > timezone.now().timestamp()


def _controls(request, session):
    owner = bool(session.owner_key) and session.owner_key == request.session.get("invigilator_key")
    return owner or _history_access(request)


def _gate(request, history=False):
    """Returns a response if the visitor must be stopped, else None."""
    passcode = _configured_passcode()
    if passcode is None:
        return render(request, "quiz/exam_gate.html", {"unconfigured": True})

    if (_history_access(request) if history else request.session.get("is_invigilator")):
        return None

    tries = request.session.get("exam_tries", 0)
    if tries >= MAX_TRIES:
        return render(request, "quiz/exam_gate.html", {"locked": True})

    if request.method == "POST" and "passcode" in request.POST:
        given = request.POST.get("passcode", "").strip()
        # compare_digest rather than == so the comparison does not finish
        # early on the first wrong character.
        if secrets.compare_digest(given.encode("utf-8"),
                                  passcode.encode("utf-8")):
            request.session["is_invigilator"] = True
            request.session["exam_history_until"] = (timezone.now() + timedelta(minutes=15)).timestamp()
            request.session["exam_tries"] = 0
            return redirect(request.path)
        request.session["exam_tries"] = tries + 1
        return render(request, "quiz/exam_gate.html",
                      {"error": "That passcode is not right.",
                       "left": MAX_TRIES - tries - 1})

    return render(request, "quiz/exam_gate.html", {})


# ----------------------------------------------------------- invigilator ---

def parse_questions(text):
    """Turn the invigilator's typed paper into question dicts.

    Format - one blank line between questions, a star on the right answer:

        Which data structure gives O(1) average lookup by key?
        - Array
        * Hash table
        - Linked list

    Deliberately forgiving: -, *, bullets and letter/number prefixes all work
    as option markers, and the star may sit anywhere before the text. The one
    strict rule is exactly one starred option per question, because a paper
    with two right answers or none is a mistake worth stopping on rather than
    guessing about.

    Returns (questions, errors). Errors are phrased for someone typing a
    paper, not someone reading a stack trace.
    """
    MARKER = re.compile(r"^(?:[-–—•]|\(?[A-Za-z0-9][).])\s*")

    def read_option(line):
        """-> (is_correct, text) or None if this is not an option line.

        Accepts a star, a marker, or both, in either order:
            * Hash table        - Paris        b) *Paris       2. * right
        A line with NEITHER a star nor a marker is not an option - that is
        what catches a stray sentence typed among the choices.
        """
        s, star = line.strip(), False
        if s.startswith("*"):
            star, s = True, s[1:].strip()
        m = MARKER.match(s)
        if m:
            s = s[m.end():].strip()
        if s.startswith("*"):
            star, s = True, s[1:].strip()
        if not s or not (star or m):
            return None
        return star, s

    questions, errors = [], []
    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]

    for n, block in enumerate(blocks, 1):
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            errors.append(f"Question {n}: no options - list them underneath, "
                          "one per line, starting with - or *")
            continue

        prompt, opts, correct = lines[0].strip(), [], []
        for line in lines[1:]:
            got = read_option(line)
            if got is None:
                errors.append(f"Question {n}: could not read the line "
                              f"“{line.strip()[:48]}” - options start "
                              "with - or *")
                continue
            starred, text = got
            if starred:
                correct.append(len(opts))
            opts.append(text)

        if len(opts) < 2:
            errors.append(f"Question {n}: needs at least two options")
        elif len(correct) == 0:
            errors.append(f"Question {n}: no right answer marked - put a * at "
                          "the start of the correct option")
        elif len(correct) > 1:
            errors.append(f"Question {n}: {len(correct)} options are starred - "
                          "mark exactly one")
        else:
            questions.append({"question": prompt, "options": opts,
                              "answer_index": correct[0]})

    if not questions and not errors:
        errors.append("No questions found.")
    return questions, errors


@never_cache
def exam_create(request):
    """Invigilator starts a session and gets a code to read to the room."""
    blocked = _gate(request)
    if blocked is not None:
        return blocked

    if request.method != "POST":
        return render(request, "quiz/exam_create.html",
                      {"sample": SAMPLE_TEXT})

    raw = request.POST.get("questions", "")
    title = request.POST.get("title", "").strip()
    questions, errors = parse_questions(raw)

    # The field is `required` in the form, so this only fires on a hand-made
    # POST - but naming an exam for them is worse than asking. The students
    # read this off their screen.
    if not title:
        errors.insert(0, "Give the exam a name.")

    if errors:
        # Hand back what they typed. Losing a paper someone just typed out
        # because of one missing star would be unforgivable.
        return render(request, "quiz/exam_create.html", {
            "errors": errors,
            "questions_text": raw,
            "title": title,
            "minutes": request.POST.get("minutes", "30"),
            "sample": SAMPLE_TEXT,
        })

    try:
        minutes = int(request.POST.get("minutes", 30) or 30)
    except (ValueError, TypeError):
        return HttpResponseBadRequest("Invalid exam duration.")
    session = ExamSession.objects.create(
        code=secrets.token_hex(3).upper(),      # e.g. 'A3F19C'
        title=title,
        minutes=max(1, min(minutes, 240)),
        owner_key=_owner_key(request),
        questions=questions,
    )

    # The passcode buys ONE session, not a logged-in browser.
    #
    # Invigilators work on shared lab machines. A browser that stays trusted
    # after the invigilator walks away is a browser any student can sit down
    # at and start an exam from. Clearing it here means the passcode is asked
    # for every single time an exam is started, which is the only moment it
    # actually matters.
    #
    # This does NOT log them out of the session they just made: the dashboard
    # and the mode controls authorise against owner_key, which is separate and
    # still in the session. They keep control of this exam, and have no
    # standing to create the next one.
    request.session.pop("is_invigilator", None)
    request.session.pop("exam_tries", None)
    return redirect("exam_dashboard", code=session.code)


@never_cache
def exam_dashboard(request, code):
    """Live grid of students. Polls exam_status for updates."""
    session = get_object_or_404(ExamSession, code=code)
    if not _controls(request, session):
        return HttpResponseForbidden("Not your session. Open Exam reports and enter the staff passcode.")
    if session.ended_at:
        return redirect("exam_results", code=code)
    return render(request, "quiz/exam_dashboard.html", {"session": session})


@require_POST
def exam_set_mode(request, code):
    """Invigilator flips the whole room, or one student, into another mode."""
    with locked_exam(code) as session:
        if not _controls(request, session):
            return HttpResponseForbidden("Not your session.")
        if session.ended_at:
            return HttpResponseBadRequest("This exam has ended.")

        mode = request.POST.get("mode", "")
        if mode not in MODES:
            return HttpResponseBadRequest("Unknown mode.")
        reason = request.POST.get("reason", "")[:200]
        attempt_id = request.POST.get("attempt")

        if attempt_id:                               # one student
            attempt = get_object_or_404(ExamAttempt, id=attempt_id, session=session)
            attempt.mode = mode
            attempt.mode_reason = reason
            attempt.save(update_fields=["mode", "mode_reason"])
        else:                                        # the whole room
            session.mode = mode
            session.mode_reason = reason
            session.save(update_fields=["mode", "mode_reason"])
            session.attempts.update(mode="", mode_reason="")   # clear overrides

        return JsonResponse({"ok": True, "mode": mode})


@never_cache
def exam_status(request, code):
    """Dashboard polls this: every student's current state."""
    session = get_object_or_404(ExamSession, code=code)
    if not _controls(request, session):
        return HttpResponseForbidden("Not your session.")
    now = timezone.now()
    due = now - timedelta(minutes=session.minutes)
    if session.attempts.filter(submitted_at__isnull=True, began_at__lte=due).exists():
        with locked_exam(code) as current:
            for attempt in current.attempts.filter(submitted_at__isnull=True, began_at__lte=due).select_related("session"):
                expire(attempt, now)
    rows = []
    for a in session.attempts.all().order_by("name"):
        last = a.last_seen
        stale = last is None or (now - last) > timedelta(seconds=20)
        rows.append({
            "id": a.id,
            "name": a.name,
            "state": "offline" if stale else a.state,
            "mode": a.mode or session.mode,
            "flags": a.flag_count,
            "submitted": a.submitted_at is not None,
        })
    return JsonResponse({
        "mode": session.mode,
        "reason": session.mode_reason,
        "students": rows,
        "ended": session.ended_at is not None,
        "results_url": reverse("exam_results", kwargs={"code": code}),
    })


# --------------------------------------------------------------- student ---

def exam_join(request, code):
    """Student enters their name and starts."""
    session = get_object_or_404(ExamSession, code=code)
    if session.ended_at:
        return render(request, "quiz/exam_closed.html", {"session": session})
    if request.method == "POST":
        name = request.POST.get("name", "").strip()[:60]
        if not name:
            return render(request, "quiz/exam_join.html",
                          {"session": session, "error": "Enter your name."})
        with locked_exam(code) as session:
            if session.ended_at:
                return render(request, "quiz/exam_closed.html", {"session": session})
            attempt = ExamAttempt.objects.create(session=session, name=name,
                student_id=request.POST.get("student_id", "").strip()[:60])
        request.session[f"attempt_{session.code}"] = attempt.id
        return redirect("exam_run", code=code)
    return render(request, "quiz/exam_join.html", {"session": session})


@never_cache
def exam_run(request, code):
    """The exam itself. Questions come from your existing quiz pipeline."""
    session = get_object_or_404(ExamSession, code=code)
    attempt = _attempt(request, session)
    if attempt is None:
        return redirect("exam_join", code=code)
    if session.ended_at or attempt.submitted_at:
        return redirect("exam_report", code=code)
    return render(request, "quiz/exam_run.html", {
        "session": session,
        "attempt": attempt,
        "questions": [{"question": q["question"], "options": q["options"]} for q in session.questions],
        "saved_answers": attempt.answers,
        "saved_events": attempt.events,
        "saved_stats": attempt.stats,
    })


@never_cache
def exam_mode(request, code):
    """Read-only control poll, including closure before sharing has started."""
    session = get_object_or_404(ExamSession, code=code)
    attempt = _attempt(request, session)
    return JsonResponse({
        "mode": (attempt.mode if attempt else "") or session.mode,
        "reason": (attempt.mode_reason if attempt else "") or session.mode_reason,
        "ended": session.ended_at is not None,
        "finished": attempt is not None and attempt.submitted_at is not None,
    })


@never_cache
@require_POST
def exam_start(request, code):
    with locked_exam(code) as session:
        attempt = _attempt(request, session)
        if attempt is None:
            return HttpResponseBadRequest("No attempt in progress.")
        if not session.ended_at and not attempt.submitted_at:
            if attempt.began_at is None:
                attempt.began_at = timezone.now()
                attempt.save(update_fields=["began_at"])
            expire(attempt)
        return _attempt_response(attempt)


def _attempt_response(attempt):
    due = deadline(attempt)
    data = {
        "ok": True,
        "total": len(attempt.session.questions), "flags": attempt.flag_count,
        "mode": attempt.mode or attempt.session.mode,
        "reason": attempt.mode_reason or attempt.session.mode_reason,
        "finished": attempt.submitted_at is not None,
        "ended": attempt.session.ended_at is not None,
        "deadline": due.isoformat() if due else None,
        "began_at": attempt.began_at.isoformat() if attempt.began_at else None,
        "revision": attempt.revision,
    }
    # Autosaving must not become a marks/answer-key oracle during the exam.
    if attempt.submitted_at is not None:
        data["score"] = attempt.score
    return JsonResponse(data)


def _save_attempt(request, code, final):
    try:
        if len(request.body) > 2 * 1024 * 1024:
            return HttpResponseBadRequest("Report is too large.")
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest("Bad payload.")
    with locked_exam(code) as session:
        attempt = _attempt(request, session)
        if attempt is None:
            return HttpResponseBadRequest("No attempt in progress.")
        # Ended/submitted attempts are immutable. Retrying a lost response
        # returns the original result, without replacing marks or timestamps.
        if session.ended_at or attempt.submitted_at:
            return _attempt_response(attempt)
        try:
            cleaned = clean_snapshot(payload, session)
            revision = payload.get("revision", attempt.revision + 1)
            if type(revision) is not int or not 1 <= revision <= 2147483647:
                raise ValueError("Invalid save revision.")
        except ValueError as error:
            return HttpResponseBadRequest(str(error))
        now = timezone.now()
        expire(attempt, now)
        if attempt.submitted_at:
            return _attempt_response(attempt)
        if attempt.began_at is None:
            if not final:
                return HttpResponseBadRequest("Start screen sharing first.")
            # Existing, already-open student tabs may predate the start API.
            attempt.began_at = attempt.started_at
        try:
            save_snapshot(attempt, payload, cleaned, now)
        except ValueError as error:
            return HttpResponseBadRequest(str(error))
        if final:
            finish(attempt, now, "submitted")
        return _attempt_response(attempt)


@never_cache
@require_POST
def exam_checkpoint(request, code):
    return _save_attempt(request, code, final=False)


@never_cache
@require_POST
def exam_submit(request, code):
    return _save_attempt(request, code, final=True)


@never_cache
def exam_history(request):
    blocked = _gate(request, history=True)
    if blocked is not None:
        return blocked
    sessions = ExamSession.objects.annotate(student_count=Count("attempts"))
    return render(request, "quiz/exam_history.html", {"sessions": sessions})


def _report_access(request, session):
    if _controls(request, session):
        return None
    return _gate(request, history=True)


@never_cache
def exam_results(request, code):
    session = get_object_or_404(ExamSession, code=code)
    blocked = _report_access(request, session)
    if blocked is not None:
        return blocked
    rows = [attempt_summary(attempt) for attempt in session.attempts.all()]
    return render(request, "quiz/exam_results.html", {
        "session": session, "rows": rows,
        "autodownload": session.ended_at is not None and request.GET.get("download") == "1",
    })


@never_cache
@require_POST
def exam_end(request, code):
    with locked_exam(code) as session:
        if not _controls(request, session):
            return HttpResponseForbidden("Not your session.")
        if session.ended_at is None:
            now = timezone.now()
            session.ended_at = now
            session.save(update_fields=["ended_at"])
            for attempt in session.attempts.select_related("session"):
                expire(attempt, now)
                finish(attempt, now, "ended")
    return redirect(reverse("exam_results", kwargs={"code": code}) + "?download=1")


@never_cache
def exam_csv(request, code):
    session = get_object_or_404(ExamSession, code=code)
    blocked = _report_access(request, session)
    if blocked is not None:
        return blocked
    if session.ended_at is None:
        return HttpResponseBadRequest("End the exam before downloading its final CSV.")
    events = request.GET.get("events") == "1"
    data = (events_csv if events else results_csv)(session, session.attempts.all())
    response = HttpResponse(data, content_type="text/csv; charset=utf-8")
    suffix = "events" if events else "results"
    response["Content-Disposition"] = f'attachment; filename="exam-{session.code}-{suffix}.csv"'
    response["X-Content-Type-Options"] = "nosniff"
    # No copy is written to media, disk, a public URL or an archive.
    return response


@never_cache
@require_POST
def exam_delete(request, code):
    with locked_exam(code) as session:
        if not _controls(request, session):
            return HttpResponseForbidden("Not your session.")
        if session.ended_at is None or request.POST.get("download_confirmed") != "yes":
            return HttpResponseBadRequest("End the exam and confirm you have saved the CSV first.")
        # Explicit user confirmation, never a side effect of downloading:
        # the web server cannot know whether a file was actually saved.
        session.delete()
    return render(request, "quiz/exam_deleted.html")


@never_cache
def exam_report(request, code):
    """What the student sees afterwards: score and their own timeline."""
    session = get_object_or_404(ExamSession, code=code)
    attempt = _attempt(request, session)
    if attempt is None:
        return redirect("exam_join", code=code)
    if attempt.submitted_at is None:
        return redirect("exam_run", code=code)
    return render(request, "quiz/exam_report.html",
                  {"session": session, "attempt": attempt, "summary": attempt_summary(attempt)})


# ----------------------------------------------------------------- utils ---

def _owner_key(request):
    """Cheap ownership token so an invigilator keeps control without login."""
    key = request.session.get("invigilator_key")
    if not key:
        key = secrets.token_hex(16)
        request.session["invigilator_key"] = key
    return key


def _attempt(request, session):
    aid = request.session.get(f"attempt_{session.code}")
    if not aid:
        return None
    return ExamAttempt.objects.filter(id=aid, session=session).first()


# ------------------------------------------------------------------ urls ---
# Add to quiz/urls.py:
#
# path("exam/new/",              views.exam_create,   name="exam_create"),
# path("exam/<str:code>/",       views.exam_join,     name="exam_join"),
# path("exam/<str:code>/run/",   views.exam_run,      name="exam_run"),
# path("exam/<str:code>/mode/",  views.exam_mode,     name="exam_mode"),
# path("exam/<str:code>/submit/",views.exam_submit,   name="exam_submit"),
# path("exam/<str:code>/report/",views.exam_report,   name="exam_report"),
# path("exam/<str:code>/board/", views.exam_dashboard,name="exam_dashboard"),
# path("exam/<str:code>/status/",views.exam_status,   name="exam_status"),
# path("exam/<str:code>/set/",   views.exam_set_mode, name="exam_set_mode"),
