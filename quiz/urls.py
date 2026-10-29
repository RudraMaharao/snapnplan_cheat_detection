from django.urls import path
from . import views
from . import exam_views
from . import exam_guides

urlpatterns = [
    path("", views.upload, name="upload"),
    path("confirm/", views.confirm, name="confirm"),
    path("retry/", views.retry, name="retry"),
    path("quiz/", views.quiz, name="quiz"),
    path("result/", views.result, name="result"),
    path("plan/", views.plan, name="plan"),
    path("exam/", views.exam, name="exam"),
    path("flashcards/", views.flashcards, name="flashcards"),

    # --- exam mode: choose online or offline ---
    path("exam-mode/",              exam_guides.exam_choose,   name="exam_choose"),
    path("exam-mode/join/",         exam_guides.exam_find,     name="exam_find"),
    path("exam-mode/online/",       exam_guides.exam_online,   name="exam_online"),
    path("exam-mode/offline/",      exam_guides.exam_offline,  name="exam_offline"),

    # --- exam mode (online) ---
    path("exam/new/",               exam_views.exam_create,    name="exam_create"),
    path("exam/history/",           exam_views.exam_history,   name="exam_history"),
    path("exam/<str:code>/",        exam_views.exam_join,      name="exam_join"),
    path("exam/<str:code>/start/",  exam_views.exam_start,     name="exam_start"),
    path("exam/<str:code>/save/",   exam_views.exam_checkpoint, name="exam_checkpoint"),
    path("exam/<str:code>/end/",    exam_views.exam_end,       name="exam_end"),
    path("exam/<str:code>/results/", exam_views.exam_results,  name="exam_results"),
    path("exam/<str:code>/csv/",    exam_views.exam_csv,       name="exam_csv"),
    path("exam/<str:code>/delete/", exam_views.exam_delete,    name="exam_delete"),
    path("exam/<str:code>/run/",    exam_views.exam_run,       name="exam_run"),
    path("exam/<str:code>/mode/",   exam_views.exam_mode,      name="exam_mode"),
    path("exam/<str:code>/submit/", exam_views.exam_submit,    name="exam_submit"),
    path("exam/<str:code>/report/", exam_views.exam_report,    name="exam_report"),
    path("exam/<str:code>/board/",  exam_views.exam_dashboard, name="exam_dashboard"),
    path("exam/<str:code>/status/", exam_views.exam_status,    name="exam_status"),
    path("exam/<str:code>/set/",    exam_views.exam_set_mode,  name="exam_set_mode"),
]
