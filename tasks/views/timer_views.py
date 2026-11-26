# Timer Flow
# Employee start/pause/resume/submit cycle.

import os
import json

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from accounts.models import Employee
from ..models import Task, TimerSession, TaskAttachment, TaskPauseLog, Notification
from ..notification_utils import send_notification
from ..activity import ActivityLog, log_activity
from django.db import transaction
from ..activity import ActivityLog, log_activity
from ..serializers import (
    TaskListSerializer, TimerSessionSerializer, TaskSubmitSerializer,
)

from .utils import _is_admin, _current_task_actor, _is_task_assignee, timezone_now


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_active_session(request):
    kind, actor = _current_task_actor(request)
    if actor is None:
        return Response({"active": False, "task": None, "task_name": None, "session": None, "active_pause": None})

    owner_filter = {"employee": actor} if kind == "employee" else {"admin": actor}
    session = (
        TimerSession.objects.filter(end_time__isnull=True, **owner_filter)
        .select_related("task")
        .first()
    )

    active_pause = (
        TaskPauseLog.objects.filter(end_time__isnull=True, **owner_filter)
        .select_related("task")
        .first()
    )

    pause_data = None
    if active_pause:
        pause_data = {
            "id": active_pause.id,
            "task": active_pause.task_id,
            "task_name": active_pause.task.task_name,
            "pause_type": active_pause.pause_type,
            "break_option": active_pause.break_option,
            "break_duration_minutes": active_pause.break_duration_minutes,
            "reason": active_pause.reason,
            "start_time": active_pause.start_time.isoformat(),
        }

    return Response({
        "active": bool(session),
        "task": session.task_id if session else None,
        "task_name": session.task.task_name if session else None,
        "session": TimerSessionSerializer(session).data if session else None,
        "active_pause": pause_data,
    })


def _get_task_owner_filter(request, task, kind, actor):
    """Resolve owner filter for timer sessions, supporting both assignee and admin oversight."""
    if _is_admin(request):
        if task.assigned_to_id:
            return {"employee": task.assigned_to}
        elif task.assigned_to_admin_id:
            return {"admin": task.assigned_to_admin}
        return {"admin": actor}
    return {"employee": actor} if kind == "employee" else {"admin": actor}


def _lock_timer_owner(owner_filter):
    """Serialize timer starts for the same employee/admin, including concurrent requests."""
    owner = next(iter(owner_filter.values()))
    owner.__class__.objects.select_for_update().get(pk=owner.pk)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def start_task(request, pk):
    kind, actor = _current_task_actor(request)
    if actor is None:
        return Response({"detail": "Only employees or admins can start a timer."}, status=status.HTTP_403_FORBIDDEN)

    task = get_object_or_404(Task, pk=pk)
    if not (_is_admin(request) or _is_task_assignee(request, task)):
        return Response({"detail": "This task isn't assigned to you."}, status=status.HTTP_403_FORBIDDEN)

    if task.task_status != Task.Status.NOT_STARTED:
        return Response({"detail": "This task has already been started. Use Resume instead."}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        owner_filter = _get_task_owner_filter(request, task, kind, actor)
        _lock_timer_owner(owner_filter)
        
        # ✅ FIX: Exclude THIS task so an orphaned session on the same task doesn't block it
        if TimerSession.objects.select_for_update().filter(end_time__isnull=True, **owner_filter).exclude(task=task).exists():
            return Response({"detail": "You already have an active timer running on another task. Pause or submit it first."}, status=status.HTTP_409_CONFLICT)

        # ✅ FIX: If there was a leftover open session on this task, close it cleanly
        task.sessions.filter(end_time__isnull=True).update(end_time=timezone_now())
        TaskPauseLog.objects.filter(task=task, end_time__isnull=True).update(end_time=timezone_now())

        session_kwargs = {"task": task, **owner_filter}
        TimerSession.objects.create(**session_kwargs)
        task.task_status = Task.Status.IN_PROGRESS
        task.save(update_fields=["task_status"])
        log_activity(task, request.user, ActivityLog.Action.STARTED, from_status="not_started", to_status="in_progress")

    return Response(TaskListSerializer(task).data)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def pause_task(request, pk):
    kind, actor = _current_task_actor(request)
    if actor is None:
        return Response({"detail": "Only employees or admins can pause a timer."}, status=status.HTTP_403_FORBIDDEN)

    task = get_object_or_404(Task, pk=pk)
    if not (_is_admin(request) or _is_task_assignee(request, task)):
        return Response({"detail": "This task isn't assigned to you."}, status=status.HTTP_403_FORBIDDEN)

    owner_filter = _get_task_owner_filter(request, task, kind, actor)
    session = TimerSession.objects.filter(task=task, end_time__isnull=True).first()
    if not session:
        # If no active session, still transition status to paused
        task.task_status = Task.Status.PAUSED
        task.save(update_fields=["task_status"])
        return Response(TaskListSerializer(task).data)

    pause_type = request.data.get("pause_type", "break")
    break_option = request.data.get("break_option", "")
    break_duration_minutes = request.data.get("break_duration_minutes")
    reason = request.data.get("reason", "").strip()

    if not reason:
        if break_option == "15_mins" or break_duration_minutes == 15:
            reason = "15 Mins Refreshment Break"
            break_duration_minutes = 15
        elif break_option == "30_mins" or break_duration_minutes == 30:
            reason = "30 Mins Lunch Break"
            break_duration_minutes = 30
        else:
            reason = "Break" if pause_type == "break" else "Paused"

    with transaction.atomic():
        session.close()
        task.task_status = Task.Status.PAUSED
        task.save(update_fields=["task_status"])
        task.recalc_total_time()

        # Close any previous open pause log for this task
        TaskPauseLog.objects.filter(task=task, end_time__isnull=True).update(end_time=timezone_now())

        pause_log = TaskPauseLog.objects.create(
            task=task,
            pause_type=pause_type,
            break_option=break_option,
            break_duration_minutes=break_duration_minutes,
            reason=reason,
            **owner_filter,
        )

        log_activity(
            task, request.user, ActivityLog.Action.PAUSED,
            from_status="in_progress", to_status="paused",
            details={
                "session_id": session.id,
                "duration_seconds": session.duration_seconds,
                "pause_type": pause_type,
                "break_option": break_option,
                "break_duration_minutes": break_duration_minutes,
                "reason": reason,
            },
        )

    data = TaskListSerializer(task).data
    data["active_pause"] = {
        "id": pause_log.id,
        "pause_type": pause_log.pause_type,
        "break_option": pause_log.break_option,
        "break_duration_minutes": pause_log.break_duration_minutes,
        "reason": pause_log.reason,
        "start_time": pause_log.start_time.isoformat(),
    }
    return Response(data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def resume_task(request, pk):
    kind, actor = _current_task_actor(request)
    if actor is None:
        return Response({"detail": "Only employees or admins can resume a timer."}, status=status.HTTP_403_FORBIDDEN)

    task = get_object_or_404(Task, pk=pk)
    if not (_is_admin(request) or _is_task_assignee(request, task)):
        return Response({"detail": "This task isn't assigned to you."}, status=status.HTTP_403_FORBIDDEN)

    if task.task_status not in (Task.Status.PAUSED, Task.Status.REWORK_NEEDED, Task.Status.IN_PROGRESS):
        return Response({"detail": "This task isn't paused or in progress, so it can't be resumed."}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        owner_filter = _get_task_owner_filter(request, task, kind, actor)
        _lock_timer_owner(owner_filter)
        if TimerSession.objects.select_for_update().filter(end_time__isnull=True, **owner_filter).exclude(task=task).exists():
            return Response({"detail": "You already have an active timer running on another task. Pause or submit it first."}, status=status.HTTP_409_CONFLICT)

        if task.task_status == Task.Status.IN_PROGRESS and TimerSession.objects.filter(
            task=task, end_time__isnull=True, **owner_filter
        ).exists():
            return Response({"detail": "This task's timer is already running."}, status=status.HTTP_409_CONFLICT)

        # Clear orphaned sessions before opening the resumed session.
        task.sessions.filter(end_time__isnull=True).update(end_time=timezone_now())
        
        # Close any open pause logs for this task
        open_pauses = TaskPauseLog.objects.filter(task=task, end_time__isnull=True)
        pause_duration = 0
        for p in open_pauses:
            p.close()
            pause_duration = p.duration_seconds or 0

        from_status = task.task_status
        session = TimerSession.objects.create(
            task=task, is_rework_session=(task.task_status == Task.Status.REWORK_NEEDED), **owner_filter
        )
        task.task_status = Task.Status.IN_PROGRESS
        task.save(update_fields=["task_status"])
        log_activity(
            task, request.user, ActivityLog.Action.RESUMED,
            from_status=from_status, to_status="in_progress",
            details={
                "session_id": session.id,
                "is_rework_session": session.is_rework_session,
                "pause_duration_seconds": pause_duration,
            },
        )

    return Response(TaskListSerializer(task).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_task(request, pk):
    kind, actor = _current_task_actor(request)
    if actor is None:
        return Response({"detail": "Only employees or admins can submit a task."}, status=status.HTTP_403_FORBIDDEN)

    task = get_object_or_404(Task, pk=pk)
    if not (_is_admin(request) or _is_task_assignee(request, task)):
        return Response({"detail": "This task isn't assigned to you."}, status=status.HTTP_403_FORBIDDEN)

    resubmission_statuses = (Task.Status.SUBMITTED, Task.Status.UNDER_REVIEW, Task.Status.RESUBMITTED)
    is_resubmission = task.task_status in resubmission_statuses
    if not is_resubmission and task.task_status not in (Task.Status.IN_PROGRESS, Task.Status.PAUSED, Task.Status.NOT_STARTED):
        return Response({"detail": "This task must be in progress or paused to submit it."}, status=status.HTTP_400_BAD_REQUEST)

    raw_delete_ids = request.data.get("delete_attachment_ids", "[]")
    try:
        parsed_delete_ids = json.loads(raw_delete_ids) if isinstance(raw_delete_ids, str) else raw_delete_ids
        if not isinstance(parsed_delete_ids, list):
            raise ValueError
        delete_attachment_ids = sorted({int(attachment_id) for attachment_id in parsed_delete_ids})
    except (TypeError, ValueError, json.JSONDecodeError):
        return Response({"detail": "Invalid attachment deletion list."}, status=status.HTTP_400_BAD_REQUEST)

    attachments_to_delete = []
    if delete_attachment_ids:
        removable = TaskAttachment.objects.filter(
            task=task,
            pk__in=delete_attachment_ids,
            is_creation_attachment=False,
        )
        if kind == "employee":
            removable = removable.filter(uploaded_by=actor)
        else:
            removable = removable.filter(uploaded_by_admin=actor)
        attachments_to_delete = list(removable)
        if len(attachments_to_delete) != len(delete_attachment_ids):
            return Response({"detail": "You can only remove attachments you uploaded."}, status=status.HTTP_403_FORBIDDEN)

    task_sheet_link = request.data.get("task_sheet_link", "").strip()
    employee_remarks = request.data.get("employee_remarks", "").strip()

    owner_filter = _get_task_owner_filter(request, task, kind, actor)

    with transaction.atomic():
        open_session = TimerSession.objects.filter(task=task, end_time__isnull=True).first()
        if open_session:
            open_session.close()

        # Close any open pause log
        TaskPauseLog.objects.filter(task=task, end_time__isnull=True).update(end_time=timezone_now())

        from_status = task.task_status
        task.task_sheet_link = task_sheet_link
        task.employee_remarks = employee_remarks
        task.submitted_date = timezone_now()
        task.task_status = Task.Status.RESUBMITTED if is_resubmission or task.rework_count > 0 else Task.Status.SUBMITTED
        task.save(update_fields=["task_sheet_link", "employee_remarks", "submitted_date", "task_status"])

        attachment_kwargs = {"uploaded_by": actor} if kind == "employee" else {"uploaded_by_admin": actor}
        from django.conf import settings
        from django.core.files.storage import FileSystemStorage
        from django.core.files.storage import default_storage
        import datetime
        from django.core.files.base import ContentFile
        local_fs = FileSystemStorage(location=settings.MEDIA_ROOT, base_url=settings.MEDIA_URL)

        for f in request.FILES.getlist("attachments"):
            ext = os.path.splitext(f.name)[1].lower() if hasattr(f, "name") else ""
            if ext in {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".3gp"}:
                return Response(
                    {"detail": f"Videos are not allowed ('{f.name}'). Only images, screenshots, docx, ppt, pdf, and excel files are supported."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            clean_name = os.path.basename(getattr(f, "name", "attachment"))
            if hasattr(f, "seek"):
                try:
                    f.seek(0)
                except Exception:
                    pass
            file_bytes = f.read()

            now = datetime.datetime.now()
            subpath = f"task_attachments/{now.strftime('%Y/%m')}/{clean_name}"
            if getattr(settings, "USE_S3_STORAGE", False):
                try:
                    saved_rel_path = default_storage.save(subpath, ContentFile(file_bytes, name=clean_name))
                except Exception as s3_err:
                    import logging
                    logging.getLogger(__name__).warning("S3 save failed (%s), fallback to local storage", s3_err)
                    saved_rel_path = local_fs.save(subpath, ContentFile(file_bytes, name=clean_name))
            else:
                saved_rel_path = local_fs.save(subpath, ContentFile(file_bytes, name=clean_name))

            # Save the database row only after storage has succeeded. Marking the
            # name committed prevents the model field from retrying the failed
            # default S3 backend during save and breaking this transaction.
            att = TaskAttachment(task=task, **attachment_kwargs)
            att.file.name = saved_rel_path
            att.file._committed = True
            att.save()

        for attachment in attachments_to_delete:
            if attachment.file:
                attachment.file.delete(save=False)
            attachment.delete()

        if open_session:
            task.recalc_total_time()

        log_activity(
            task, request.user, ActivityLog.Action.SUBMITTED,
            from_status=from_status, to_status=task.task_status,
            details={"task_sheet_link": task.task_sheet_link, "attachment_count": len(request.FILES.getlist("attachments"))},
        )

        # Notify Admin of submission
        submitter_name = task.assignee_name or "Assignee"
        from accounts.models import Admin

        if task.assigned_by_admin:
            send_notification(
                recipient=task.assigned_by_admin,
                title="Task Submitted for Review",
                message=f"{submitter_name} submitted task: '{task.task_name}' for review.",
                event_type=Notification.EventType.TASK_SUBMITTED,
                action_url="/admin/review_tasks",
                sender_name=submitter_name,
            )
        else:
            for adm in Admin.objects.filter(is_active=True):
                send_notification(
                    recipient=adm,
                    title="Task Submitted for Review",
                    message=f"{submitter_name} submitted task: '{task.task_name}' for review.",
                    event_type=Notification.EventType.TASK_SUBMITTED,
                    action_url="/admin/review_tasks",
                    sender_name=submitter_name,
                )

    return Response(TaskListSerializer(task).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_task_sessions(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if not _is_admin(request) and not _is_task_assignee(request, task):
        return Response({"detail": "You can't view sessions for this task."}, status=status.HTTP_403_FORBIDDEN)
    sessions = task.sessions.all()
    return Response(TimerSessionSerializer(sessions, many=True).data)
