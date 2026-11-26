# tasks/recurring.py
import datetime
from datetime import timedelta
from django.db.models import Max, Q
from django.utils import timezone
from tasks.models import Task, RecurringTaskDefinition
from ..serializers import TaskListSerializer, RecurringTaskDefinitionSerializer, RecurringTaskDefinitionCreateSerializer
from .utils import _is_admin, _current_employee, _is_tl, _can_manage_tasks, task_list_queryset

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404

def generate_recurring_tasks(as_of_date=None):
    as_of_date = as_of_date or timezone.localdate()

    defs = RecurringTaskDefinition.objects.filter(
        is_active=True,
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=as_of_date))

    for d in defs:
        # A future occurrence is created up front so the recurring task appears
        # immediately. Ignore it when advancing the generator through today.
        last_generated = d.generated_tasks.filter(
            generated_for_date__lte=as_of_date
        ).aggregate(m=Max("generated_for_date"))["m"]
        cursor = (last_generated + timedelta(days=1)) if last_generated else d.start_date
        walk_until = min(as_of_date, d.end_date) if d.end_date else as_of_date
        
        # Set of dates excluded by Admin / TL
        excluded = set(d.excluded_dates or [])

        while cursor <= walk_until:
            cursor_str = cursor.isoformat()
            if cursor.weekday() in d.weekdays and cursor_str not in excluded:
                Task.objects.get_or_create(
                    recurring_source=d,
                    generated_for_date=cursor,
                    defaults=dict(
                        project_name=d.project_name, 
                        department=d.department,
                        task_name=d.task_name,
                        task_details=d.task_details,
                        assigned_to=d.assigned_to,
                        assigned_to_admin=d.assigned_to_admin,   # NEW — carry the admin FK through too
                        priority=d.priority,
                        allotted_time=d.allotted_time,
                        due_date=cursor,
                        assigned_by_admin=d.assigned_by_admin,
                        assigned_by_employee=d.assigned_by_employee,
                        created_by_admin=d.assigned_by_admin,
                        created_by_employee=d.assigned_by_employee,
                        task_status=Task.Status.NOT_STARTED,
                    ),
                )
            cursor += timedelta(days=1)

        # Keep one upcoming occurrence visible even when the start date or the
        # next selected weekday is in the future.
        weekdays = set(d.weekdays or [])
        if weekdays:
            next_date = max(as_of_date, d.start_date)
            while (
                (not d.end_date or next_date <= d.end_date)
                and (next_date.weekday() not in weekdays or next_date.isoformat() in excluded)
            ):
                next_date += timedelta(days=1)
            if not d.end_date or next_date <= d.end_date:
                Task.objects.get_or_create(
                    recurring_source=d,
                    generated_for_date=next_date,
                    defaults=dict(
                        project_name=d.project_name,
                        department=d.department,
                        task_name=d.task_name,
                        task_details=d.task_details,
                        assigned_to=d.assigned_to,
                        assigned_to_admin=d.assigned_to_admin,
                        priority=d.priority,
                        allotted_time=d.allotted_time,
                        due_date=next_date,
                        assigned_by_admin=d.assigned_by_admin,
                        assigned_by_employee=d.assigned_by_employee,
                        created_by_admin=d.assigned_by_admin,
                        created_by_employee=d.assigned_by_employee,
                        task_status=Task.Status.NOT_STARTED,
                    ),
                )

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_recurring_date(request, pk):
    """
    POST /api/tasks/recurring/<pk>/toggle_date/
    Payload: { "date": "YYYY-MM-DD" }
    Toggles a specific date between excluded (skipped) and included.
    If unticked (excluded), removes any unstarted task that was already generated for that day.
    """
    definition = get_object_or_404(RecurringTaskDefinition, pk=pk)
    if not _owns_recurring_for_management(request, definition):
        return Response({"detail": "You do not have permission to edit this schedule."}, status=status.HTTP_403_FORBIDDEN)
    target_date = request.data.get("date")
    if not target_date:
        return Response({"detail": "Date is required (YYYY-MM-DD)."}, status=status.HTTP_400_BAD_REQUEST)
    excluded = list(definition.excluded_dates or [])
    if target_date in excluded:
        # Untick -> Re-tick (Include again)
        excluded.remove(target_date)
        definition.excluded_dates = excluded
        definition.save(update_fields=["excluded_dates"])
        generate_recurring_tasks()
        action_taken = "included"
    else:
        # Tick -> Untick (Exclude / Skip this date)
        excluded.append(target_date)
        definition.excluded_dates = excluded
        definition.save(update_fields=["excluded_dates"])
        # Delete / clear any unstarted task generated for that date
        try:
            parsed_date = datetime.date.fromisoformat(target_date)
            Task.objects.filter(
                recurring_source=definition,
                generated_for_date=parsed_date,
                task_status=Task.Status.NOT_STARTED,
            ).delete()
        except Exception:
            pass
        action_taken = "excluded"
    return Response({
        "status": "success",
        "action": action_taken,
        "date": target_date,
        "definition": RecurringTaskDefinitionSerializer(definition).data,
    })
    
# ── Recurring task definition views ────────────────────────────────────────
# Create/list/stop recurring task definitions.

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_recurring_tasks(request):
    """GET /api/tasks/recurring/ — admin-only, sitewide management list."""
    if not _is_admin(request):
        return Response({"detail": "Admin only."}, status=status.HTTP_403_FORBIDDEN)
    defs = RecurringTaskDefinition.objects.all()
    return Response(RecurringTaskDefinitionSerializer(defs, many=True).data)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def recurring_task_detail(request, pk):
    """Read or update a recurring schedule and reconcile pending occurrences."""
    if not _is_admin(request):
        return Response({"detail": "Admin only."}, status=status.HTTP_403_FORBIDDEN)

    definition = get_object_or_404(RecurringTaskDefinition, pk=pk)
    if request.method == "GET":
        return Response(RecurringTaskDefinitionSerializer(definition).data)

    serializer = RecurringTaskDefinitionCreateSerializer(definition, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    definition = serializer.save()

    today = timezone.localdate()
    pending = definition.generated_tasks.filter(
        task_status=Task.Status.NOT_STARTED,
        generated_for_date__gte=today,
    )
    allowed_weekdays = set(definition.weekdays or [])
    excluded = set(definition.excluded_dates or [])
    for task in pending:
        occurrence_date = task.generated_for_date
        if (
            occurrence_date < definition.start_date
            or (definition.end_date and occurrence_date > definition.end_date)
            or occurrence_date.weekday() not in allowed_weekdays
            or occurrence_date.isoformat() in excluded
        ):
            task.delete()
            continue
        task.project_name = definition.project_name
        task.department = definition.department
        task.task_name = definition.task_name
        task.task_details = definition.task_details
        task.assigned_to = definition.assigned_to
        task.assigned_to_admin = definition.assigned_to_admin
        task.priority = definition.priority
        task.allotted_time = definition.allotted_time
        task.save(update_fields=[
            "project_name", "department", "task_name", "task_details", "assigned_to",
            "assigned_to_admin", "priority", "allotted_time",
        ])

    generate_recurring_tasks()
    return Response(RecurringTaskDefinitionSerializer(definition).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_my_recurring_tasks(request):
    """Deprecated: TL concept removed."""
    return Response([])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_recurring_task(request):
    employee = None if _is_admin(request) else _current_employee(request)
    if not _is_admin(request) and employee is None:
        return Response({"detail": "Only admins and employees can create recurring tasks."}, status=status.HTTP_403_FORBIDDEN)

    serializer = RecurringTaskDefinitionCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    if _is_admin(request):
        definition = serializer.save(assigned_by_admin=request.user.instance)
    else:
        definition = serializer.save(assigned_by_employee=employee)
    generate_recurring_tasks()

    return Response(RecurringTaskDefinitionSerializer(definition).data, status=status.HTTP_201_CREATED)

def _owns_recurring_for_management(request, definition):
    """Admin-only."""
    return _is_admin(request)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def stop_recurring_task(request, pk):
    """
    POST /api/tasks/recurring/<id>/stop/
    Admin can stop any rule.
    """
    definition = get_object_or_404(RecurringTaskDefinition, pk=pk)
    if not _is_admin(request):
        return Response({"detail": "Only admins can stop recurring tasks."}, status=status.HTTP_403_FORBIDDEN)

    definition.is_active = False
    definition.save(update_fields=["is_active"])
    return Response(RecurringTaskDefinitionSerializer(definition).data)


# ── Lazy-fallback hooks for the listing views ───────────────────────────────
# tasks/views/task_crud.py — same file as get_all_tasks / get_my_tasks / get_tl_tasks

# tasks/views/recurring.py
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_all_tasks(request):
    generate_recurring_tasks()
    tasks = task_list_queryset()
    if _is_admin(request):
        tasks = tasks.order_by("-assigned_date", "-id")
    else:
        employee = _current_employee(request)
        if employee is None:
            return Response([], status=status.HTTP_200_OK)
        from django.db.models import F
        tasks = tasks.filter(assigned_to=employee).order_by(
            F("due_date").asc(nulls_last=True), F("due_time").asc(nulls_last=True), "id"
        )
    return Response(TaskListSerializer(tasks, many=True).data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_my_tasks(request):
    generate_recurring_tasks()
    employee = _current_employee(request)
    if employee is None:
        return Response({"detail": "Employees only."}, status=status.HTTP_403_FORBIDDEN)
    
    mode = request.query_params.get("mode", "").strip().lower()
    from django.db.models import F
    tasks = task_list_queryset().filter(assigned_to=employee).order_by(
        F("due_date").asc(nulls_last=True), F("due_time").asc(nulls_last=True), "id"
    )
    if mode == "normal":
        tasks = tasks.filter(recurring_source__isnull=True)
    elif mode == "recurring":
        tasks = tasks.filter(recurring_source__isnull=False)
    return Response(TaskListSerializer(tasks, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_tl_tasks(request):
    """Deprecated: TL concept removed."""
    return Response([])

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_assigned_recurring_tasks(request):
    """
    GET /api/tasks/recurring/assigned_to_me/
    Returns active scheduled task rules assigned to the currently logged in user (Admin or Employee).
    """
    generate_recurring_tasks()
    if _is_admin(request):
        defs = RecurringTaskDefinition.objects.filter(
            assigned_to_admin=request.user.instance, is_active=True
        )
    else:
        employee = _current_employee(request)
        if not employee:
            return Response([], status=status.HTTP_200_OK)
        defs = RecurringTaskDefinition.objects.filter(
            assigned_to=employee, is_active=True
        )
    return Response(RecurringTaskDefinitionSerializer(defs, many=True).data)
