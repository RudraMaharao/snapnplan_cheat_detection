# Online exam reports and early ending

From **Online exam → Exam reports**, staff can recover an exam using the existing
staff passcode. History access expires after 15 minutes; the browser that created
an exam keeps control of that exam as before. The report list contains only records
that have not been deleted.

## At the end of an exam

1. Click **End exam & download CSV** on the dashboard or results page. Confirm the
   prompt. This closes joining, freezes results and starts a CSV download.
2. Open and check the downloaded results CSV. It contains one row per student:
   name, optional roll number, marks, percentage, answered/wrong/unanswered counts,
   joined/start/finish timestamps, time taken, last sync, window/tab/share-stop
   flags, monitoring statistics, answers and complete events as JSON. Timestamps
   use UTC. A separate event timeline CSV provides one row per event.
3. If downloading was blocked, use **Download results CSV** to try again.
4. When ready, select **I have downloaded and checked the results CSV** and click
   **Confirm download & delete exam records**. Confirm the deletion prompt. Only
   then are that exam, its questions, attempts, marks and events deleted from the
   application's database. Downloading alone never deletes anything.

CSVs are generated in memory for the authenticated download response. There is no
CSV archive on the server. Undeleted exam records remain available until staff
confirm deletion; copies in existing external server backups are unaffected.

## What early ending saves

Students' answers and monitoring snapshots are saved every five seconds, after
answer changes, and on a best-effort basis when a page closes. Connected students
stop on the next poll; offline students are finalized from their last saved data.
Answers received after ending cannot replace frozen results. Reports expose the
last save timestamp so incomplete monitoring data is visible.

Each student's server timer starts once screen sharing is ready and survives a
reload. Server expiry uses the latest saved answers. A room stays open until the
invigilator ends it (the dashboard indicates when all students have finished).
Students who never started have no invented duration. Old submitted attempts
retain their data and label time based on joining as a legacy estimate.

Flags are monitoring signals for staff review, not automatic cheating decisions.
No screenshot/video upload is added. Existing shared-passcode limitations still
apply; account-based staff authorization would be a separate change.

## Deployment

Deploy when no exam is in progress: already-open student tabs need to reload for
the new automatic saving and end controls. Apply migrations and collect static
files before restarting the service:

```
cd /srv/snapnplan/app
sudo -u snapnplan -g www-data /srv/snapnplan/venv/bin/python manage.py migrate --noinput
sudo -u snapnplan -g www-data /srv/snapnplan/venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart snapnplan
```

Migration `0002_exam_results_and_closure` adds fields; it does not delete existing
exams. Explicitly keep BigAutoField to match migration 0001 across Django versions.
For application rollback, deploy the prior code without reversing the migration;
its existing fields remain compatible.

## Validation

```
python manage.py test quiz
python manage.py makemigrations --check --dry-run
node --test tests/*.mjs
```

Backend tests cover authorization/CSRF, automatic-download navigation, CSV fields
and formula safety, malformed and out-of-order autosaves, deadlines, idempotent
submission/ending, frozen marks, legacy attempts, and explicitly confirmed deletion.
JavaScript tests cover non-destructive snapshots, ongoing events, and stopping/retry
behavior. The ML model is unchanged and is not required for these lifecycle tests.
