import csv
import io
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .exam_records import attempt_summary
from .models import ExamAttempt, ExamSession


@override_settings(EXAM_PASSCODE="test-staff-passcode")
class ExamRecordsTests(TestCase):
    def setUp(self):
        self.exam = ExamSession.objects.create(
            code="ABC123", title="Data structures", minutes=30, owner_key="teacher",
            questions=[{"question": "Lookup?", "options": ["Array", "Hash table"], "answer_index": 1},
                       {"question": "Traversal?", "options": ["In-order", "Pre-order"], "answer_index": 0}])
        session = self.client.session
        session["invigilator_key"] = "teacher"
        session.save()
        self.student = Client()
        self.student.post(self.url("exam_join"), {"name": "Aditi, S", "student_id": "DS-101"})
        self.attempt = ExamAttempt.objects.get(session=self.exam)
        self.student.post(self.url("exam_start"))
        self.attempt.refresh_from_db()
        self.events = [
            {"type": "other_window_visible", "seconds": 12, "elapsed": 15, "mode": "monitoring", "at": "2026-10-29T12:00:00Z", "note": "window"},
            {"type": "left_exam_tab", "seconds": 5, "elapsed": 22, "mode": "monitoring"},
            {"type": "sharing_stopped", "seconds": 0, "elapsed": 23, "mode": "monitoring"},
        ]

    def url(self, name):
        return reverse(name, kwargs={"code": self.exam.code})

    def payload(self, revision=1, answers=None):
        return {"answers": [1, None] if answers is None else answers,
                "state": "flagged", "revision": revision,
                "report": {"events": self.events, "stats": {"monitored_s": 30, "frames_checked": 12}}}

    def save(self, payload=None, final=False):
        return self.student.post(self.url("exam_submit" if final else "exam_checkpoint"),
                                 data=json.dumps(payload or self.payload()), content_type="application/json")

    def end(self):
        return self.client.post(self.url("exam_end"))

    def csv_rows(self, events=False):
        response = self.client.get(self.url("exam_csv"), {"events": "1"} if events else {})
        self.assertEqual(response.status_code, 200)
        return response, list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))

    def test_checkpoint_saves_answers_flags_and_roll_number_before_submit(self):
        self.assertEqual(self.save().status_code, 200)
        self.attempt.refresh_from_db()
        self.assertEqual((self.attempt.score, self.attempt.flag_count, self.attempt.student_id), (1, 3, "DS-101"))
        self.assertIsNone(self.attempt.submitted_at)
        self.assertIsNotNone(self.attempt.saved_at)
        self.assertEqual(self.attempt.events[0]["seconds"], 12)

    def test_start_is_idempotent_and_does_not_reset_the_timer(self):
        original = self.attempt.began_at
        self.student.post(self.url("exam_start"))
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.began_at, original)

    def test_autosave_does_not_reveal_marks_until_submission(self):
        self.assertNotIn("score", self.save().json())
        self.assertEqual(self.save(self.payload(revision=2), final=True).json()["score"], 1)

    def test_stale_final_submission_freezes_the_newest_saved_answers(self):
        self.save(self.payload(revision=5, answers=[1, 0]))
        response = self.save(self.payload(revision=4, answers=[0, 1]), final=True)
        self.assertTrue(response.json()["finished"])
        self.assertEqual(response.json()["score"], 2)

    def test_end_freezes_last_saved_results_for_offline_students(self):
        self.save()
        response = self.end()
        self.assertRedirects(response, self.url("exam_results") + "?download=1")
        self.exam.refresh_from_db(); self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.submitted_at, self.exam.ended_at)
        self.assertEqual(self.attempt.finish_reason, "ended")
        self.assertEqual(self.attempt.score, 1)
        self.assertEqual(self.attempt.flag_count, 3)
        self.assertTrue(self.student.get(self.url("exam_mode")).json()["ended"])

    def test_end_is_idempotent_and_preserves_voluntary_submission(self):
        self.save(final=True)
        self.attempt.refresh_from_db(); submitted = self.attempt.submitted_at
        self.end(); self.exam.refresh_from_db(); ended = self.exam.ended_at
        self.end(); self.exam.refresh_from_db(); self.attempt.refresh_from_db()
        self.assertEqual(self.exam.ended_at, ended)
        self.assertEqual(self.attempt.submitted_at, submitted)
        self.assertEqual(self.attempt.finish_reason, "submitted")

    def test_late_and_duplicate_requests_cannot_change_final_marks(self):
        self.save(); self.end()
        for final in (True, False):
            response = self.save(self.payload(revision=10, answers=[1, 0]), final=final)
            self.assertTrue(response.json()["finished"])
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.score, 1)
        self.assertEqual(self.attempt.answers, [1, None])

    def test_out_of_order_autosave_cannot_replace_newer_answers(self):
        self.save(self.payload(revision=5, answers=[1, 0]))
        self.save(self.payload(revision=4, answers=[0, 1]))
        self.attempt.refresh_from_db()
        self.assertEqual((self.attempt.revision, self.attempt.score), (5, 2))

    def test_duplicate_submission_keeps_original_timestamp(self):
        self.save(final=True)
        self.attempt.refresh_from_db(); before = self.attempt.submitted_at
        self.save(self.payload(revision=2, answers=[1, 0]), final=True)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.submitted_at, before)
        self.assertEqual(self.attempt.score, 1)

    def test_closed_exam_blocks_new_joins_and_reopening_questions(self):
        self.end()
        self.assertContains(Client().post(self.url("exam_join"), {"name": "Late"}), "This exam has ended")
        self.assertEqual(self.exam.attempts.count(), 1)
        self.assertRedirects(self.student.get(self.url("exam_run")), self.url("exam_report"))
        self.assertEqual(self.client.post(self.url("exam_set_mode"), {"mode": "paused"}).status_code, 400)

    def test_timeout_freezes_saved_answers_at_the_server_deadline(self):
        self.save()
        began = timezone.now() - timedelta(minutes=31)
        ExamAttempt.objects.filter(pk=self.attempt.pk).update(began_at=began)
        response = self.save(self.payload(revision=2, answers=[1, 0]))
        self.assertTrue(response.json()["finished"])
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.submitted_at, began + timedelta(minutes=30))
        self.assertEqual((self.attempt.finish_reason, self.attempt.score), ("timeout", 1))

    def test_dashboard_expires_an_offline_attempt(self):
        began = timezone.now() - timedelta(minutes=31)
        ExamAttempt.objects.filter(pk=self.attempt.pk).update(began_at=began)
        response = self.client.get(self.url("exam_status"))
        self.assertTrue(response.json()["students"][0]["submitted"])
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.finish_reason, "timeout")

    def test_csv_contains_all_students_metrics_and_full_event_log(self):
        began = timezone.now() - timedelta(seconds=125)
        ExamAttempt.objects.filter(pk=self.attempt.pk).update(began_at=began)
        self.save(); self.end()
        response, rows = self.csv_rows()
        self.assertIn('filename="exam-ABC123-results.csv"', response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["Student name"], "Aditi, S")
        self.assertEqual(row["Student ID / roll number"], "DS-101")
        self.assertEqual((row["Marks"], row["Maximum marks"], row["Percentage"]), ("1", "2", "50.0"))
        self.assertEqual((row["Flags"], row["Window flags"], row["Tab switches"], row["Sharing stops"]), ("3", "1", "1", "1"))
        self.assertGreaterEqual(int(row["Time taken (seconds)"]), 125)
        self.assertEqual(len(json.loads(row["Events (JSON)"])), 3)
        self.assertEqual(json.loads(row["Answers (JSON, zero-based option indexes)"]), [1, None])

    def test_csv_never_writes_an_archive_or_deletes_records(self):
        self.save(); self.end()
        with patch("builtins.open", side_effect=AssertionError("CSV must remain in memory")):
            self.csv_rows()
        self.assertTrue(ExamSession.objects.filter(pk=self.exam.pk).exists())
        self.assertTrue(ExamAttempt.objects.filter(pk=self.attempt.pk).exists())

    def test_flat_events_csv_and_formula_injection_protection(self):
        self.save(); self.end()
        ExamAttempt.objects.filter(pk=self.attempt.pk).update(name='=HYPERLINK("evil")', student_id=" +123")
        _, rows = self.csv_rows()
        self.assertTrue(rows[0]["Student name"].startswith("'="))
        self.assertTrue(rows[0]["Student ID / roll number"].startswith("'"))
        _, events = self.csv_rows(events=True)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["Event type"], "other_window_visible")
        self.assertTrue(events[0]["Student name"].startswith("'="))

    def test_unstarted_student_is_reported_without_inventing_duration(self):
        other = ExamAttempt.objects.create(session=self.exam, name="Never started")
        self.end(); other.refresh_from_db()
        summary = attempt_summary(other)
        self.assertIsNone(summary["seconds"])
        self.assertEqual(summary["status"], "Ended before starting")

    def test_legacy_attempt_duration_is_labelled_as_an_estimate(self):
        old = ExamAttempt.objects.create(session=self.exam, name="Old", submitted_at=timezone.now())
        self.assertEqual(attempt_summary(old)["time_basis"], "joined time (legacy estimate)")

    def test_report_and_csv_are_staff_only_even_if_student_knows_exam_code(self):
        self.end()
        for name in ("exam_results", "exam_csv"):
            response = self.student.get(self.url(name))
            self.assertContains(response, "Invigilator access")
            self.assertNotContains(response, "DS-101")
        self.assertEqual(self.student.get(self.url("exam_status")).status_code, 403)
        self.assertEqual(self.student.post(self.url("exam_end")).status_code, 403)
        self.assertEqual(self.student.post(self.url("exam_delete"), {"download_confirmed": "yes"}).status_code, 403)

    def test_staff_can_recover_reports_from_another_browser(self):
        self.save(); self.end(); staff = Client()
        response = staff.post(reverse("exam_history"), {"passcode": "test-staff-passcode"})
        self.assertRedirects(response, reverse("exam_history"))
        self.assertContains(staff.get(self.url("exam_results")), "DS-101")
        self.assertEqual(staff.get(self.url("exam_csv")).status_code, 200)

    def test_history_access_expires_and_wrong_passcode_cannot_grant_access(self):
        staff = Client()
        staff.post(reverse("exam_history"), {"passcode": "wrong"})
        self.assertNotIn("exam_history_until", staff.session)
        session = staff.session; session["exam_history_until"] = 1; session.save()
        self.assertContains(staff.get(reverse("exam_history")), "Invigilator access")

    def test_delete_requires_post_closed_exam_and_explicit_download_confirmation(self):
        self.assertEqual(self.client.get(self.url("exam_delete")).status_code, 405)
        self.assertEqual(self.client.post(self.url("exam_delete"), {"download_confirmed": "yes"}).status_code, 400)
        self.end()
        self.assertEqual(self.client.post(self.url("exam_delete")).status_code, 400)
        self.assertTrue(ExamSession.objects.filter(pk=self.exam.pk).exists())
        other = ExamSession.objects.create(code="OTHER", owner_key="teacher")
        response = self.client.post(self.url("exam_delete"), {"download_confirmed": "yes"})
        self.assertContains(response, "Exam records deleted")
        self.assertFalse(ExamSession.objects.filter(pk=self.exam.pk).exists())
        self.assertFalse(ExamAttempt.objects.filter(pk=self.attempt.pk).exists())
        self.assertTrue(ExamSession.objects.filter(pk=other.pk).exists())
        self.assertEqual(self.client.get(self.url("exam_csv")).status_code, 404)

    def test_end_redirect_requests_automatic_download_but_delete_is_never_automatic(self):
        response = self.end()
        self.assertEqual(response.url, self.url("exam_results") + "?download=1")
        page = self.client.get(response.url)
        self.assertContains(page, 'document.getElementById("download-csv").click()')
        self.assertContains(page, 'name="download_confirmed"')
        self.assertTrue(ExamSession.objects.filter(pk=self.exam.pk).exists())

    def test_csv_cannot_be_downloaded_before_exam_is_frozen(self):
        self.assertEqual(self.client.get(self.url("exam_csv")).status_code, 400)

    def test_question_page_does_not_expose_answer_key_and_restores_saved_answers(self):
        self.save()
        page = self.student.get(self.url("exam_run"))
        self.assertNotContains(page, '"answer_index"')
        self.assertContains(page, '[1, null]')
        self.assertIn("no-store", page["Cache-Control"])

    def test_malformed_payload_does_not_overwrite_saved_data(self):
        self.save()
        malformed = [[], {"answers": "bad"}, {"answers": [True]}, {"answers": [99]},
                     {"answers": [None], "report": []},
                     {"answers": [None], "report": {"events": [42]}},
                     {"answers": [None], "report": {"events": [{"type": "x", "seconds": -1}]}},
                     {"answers": [None], "report": {"stats": {"frames_checked": float("nan")}}},
                     {"answers": [None], "revision": "bad"}]
        for payload in malformed:
            with self.subTest(payload=payload):
                response = self.student.post(self.url("exam_checkpoint"), json.dumps(payload), content_type="application/json")
                self.assertEqual(response.status_code, 400)
        self.attempt.refresh_from_db()
        self.assertEqual((self.attempt.score, self.attempt.flag_count), (1, 3))

    def test_cross_exam_attempt_cannot_be_submitted(self):
        other = ExamSession.objects.create(code="OTHER", owner_key="other")
        response = self.student.post(reverse("exam_checkpoint", kwargs={"code": other.code}), json.dumps(self.payload()), content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_post_controls_and_autosave_require_csrf(self):
        protected = Client(enforce_csrf_checks=True)
        protected.cookies = self.client.cookies
        self.assertEqual(protected.post(self.url("exam_end")).status_code, 403)
        protected.cookies = self.student.cookies
        self.assertEqual(protected.post(self.url("exam_checkpoint"), json.dumps(self.payload()), content_type="application/json").status_code, 403)

    def test_html_in_student_names_is_escaped_in_report(self):
        ExamAttempt.objects.filter(pk=self.attempt.pk).update(name='<img src=x onerror="alert(1)">')
        page = self.client.get(self.url("exam_results"))
        self.assertNotContains(page, '<img src=x onerror="alert(1)">')
        self.assertContains(page, "&lt;img")
