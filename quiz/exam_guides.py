"""Exam mode landing pages.

The Exam mode link opens a choice between two kinds of exam:

  * Online exam      - runs on this site over the internet (exam_views.py).
  * Offline lab exam - runs on a college lab network with no internet. The
                       teacher downloads a kit that turns one lab PC into the
                       exam server; this site only hosts the instructions and
                       the download.

Each choice has its own step-by-step page. Students also land here, so the
choice page has a box for joining an online exam with its code.
"""
import re

from django.conf import settings
from django.shortcuts import redirect, render

from .models import ExamSession


def exam_choose(request):
    return render(request, "quiz/exam_mode.html", {})


def exam_find(request):
    """Student typed a code on the Exam mode page: send them to that exam."""
    code = re.sub(r"[\s-]", "", request.GET.get("code", "")).upper()[:8]
    if not code:
        error = "Type the code your invigilator read out."
    elif ExamSession.objects.filter(code=code).exists():
        return redirect("exam_join", code=code)
    else:
        error = f"No exam uses the code {code}. Check it with your invigilator."
    return render(request, "quiz/exam_mode.html",
                  {"join_error": error, "join_code": code})


def exam_online(request):
    return render(request, "quiz/exam_online.html",
                  {"site": request.get_host()})


def exam_offline(request):
    kit = {
        "version": getattr(settings, "LAN_KIT_VERSION", "2.2"),
        "url": getattr(settings, "LAN_KIT_URL", ""),
        "python_url": getattr(settings, "LAN_KIT_PYTHON_URL", ""),
        "sha256": getattr(settings, "LAN_KIT_SHA256", ""),
        "size_mb": getattr(settings, "LAN_KIT_SIZE_MB", ""),
    }
    return render(request, "quiz/exam_offline.html", {"kit": kit})
