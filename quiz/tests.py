import os
import subprocess
import sys
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse

from .models import ExamSession

KIT = "https://snapnplan-downloads.s3.ap-south-1.amazonaws.com/kit/SnapNPlan-Offline-Exam-Kit-v2.2.zip"
PY = "https://snapnplan-downloads.s3.ap-south-1.amazonaws.com/kit/python-3.14.8-amd64.exe"


class ExamModePageTests(TestCase):
    def test_exam_mode_link_opens_the_choice(self):
        page = self.client.get(reverse("upload"))
        self.assertContains(page, f'href="{reverse("exam_choose")}">Exam mode</a>')
        choice = self.client.get(reverse("exam_choose"))
        self.assertContains(choice, "Online exam")
        self.assertContains(choice, "Offline lab exam")
        self.assertContains(choice, f'href="{reverse("exam_online")}"')
        self.assertContains(choice, f'href="{reverse("exam_offline")}"')
        self.assertContains(choice, f'href="{reverse("exam_create")}"')

    def test_students_join_an_online_exam_with_its_code(self):
        ExamSession.objects.create(code="A3F19C", title="Midterm", owner_key="x")
        page = self.client.get(reverse("exam_find"), {"code": " a3f-19c "})
        self.assertRedirects(page, reverse("exam_join", kwargs={"code": "A3F19C"}))

    def test_unknown_or_missing_code_explains_what_to_do(self):
        page = self.client.get(reverse("exam_find"), {"code": "<i>z</i>"})
        self.assertContains(page, "No exam uses the code &lt;I&gt;Z&lt;/I&gt;.")
        self.assertNotContains(page, "<I>Z")
        self.assertContains(self.client.get(reverse("exam_find")),
                            "Type the code your invigilator read out.")

    def test_online_steps_lead_to_the_original_exam_flow(self):
        page = self.client.get(reverse("exam_online"))
        self.assertContains(page, f'href="{reverse("exam_create")}"', count=2)
        self.assertContains(page, "testserver")

    @override_settings(LAN_KIT_URL=KIT, LAN_KIT_PYTHON_URL=PY,
                       LAN_KIT_SHA256="ab" * 32, LAN_KIT_SIZE_MB="51")
    def test_offline_steps_link_the_kit_and_python_installer(self):
        page = self.client.get(reverse("exam_offline"))
        self.assertContains(page, f'href="{KIT}"', count=2)
        self.assertContains(page, f'href="{PY}"', count=1)
        self.assertContains(page, "ab" * 32)
        self.assertContains(page, "51&nbsp;MB")
        self.assertContains(page, "SETUP-TEACHER-PC.bat")

    @override_settings(LAN_KIT_URL="", LAN_KIT_PYTHON_URL="", LAN_KIT_SHA256="")
    def test_offline_steps_without_an_uploaded_kit(self):
        page = self.client.get(reverse("exam_offline"))
        self.assertContains(page, "The download link appears here once")
        self.assertNotContains(page, "Download the kit</a>")

    def test_original_exam_routes_still_work(self):
        self.assertEqual(self.client.get(reverse("exam_create")).status_code, 200)
        ExamSession.objects.create(code="B4C5D6", title="Quiz", owner_key="x")
        self.assertEqual(self.client.get(
            reverse("exam_join", kwargs={"code": "B4C5D6"})).status_code, 200)


class ProductionSettingsTests(TestCase):
    def run_settings(self, **env):
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("DJANGO_", "SNAPNPLAN_"))}
        clean.update(DJANGO_SETTINGS_MODULE="config.settings", **env)
        code = ("import django; django.setup(); from django.conf import settings as s; "
                "print(s.DEBUG, s.ALLOWED_HOSTS, s.CSRF_TRUSTED_ORIGINS, "
                "getattr(s, 'SECURE_PROXY_SSL_HEADER', None), s.MEDIA_ROOT, s.STATIC_ROOT)")
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parent.parent, env=clean)

    def test_debug_off_requires_a_secret_key(self):
        result = self.run_settings(DJANGO_DEBUG="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set DJANGO_SECRET_KEY", result.stderr)

    def test_server_values_come_from_the_environment(self):
        result = self.run_settings(
            DJANGO_DEBUG="0", DJANGO_SECRET_KEY="x" * 50, DJANGO_HTTPS="1",
            DJANGO_ALLOWED_HOSTS="snapnplan.ddns.net",
            DJANGO_CSRF_TRUSTED_ORIGINS="https://snapnplan.ddns.net",
            SNAPNPLAN_DATA_DIR="/srv/snapnplan/data")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split("\n")[-2],
                         "False ['snapnplan.ddns.net'] ['https://snapnplan.ddns.net'] "
                         "('HTTP_X_FORWARDED_PROTO', 'https') /srv/snapnplan/data/media "
                         "/srv/snapnplan/data/staticfiles")
