# Task CRUD & Listing
# Core task creation, assignment, and listing across roles.

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from accounts.models import Employee, Admin
from ..models import Task, TaskMaster, TaskAttachment, Notification
from ..serializers import (
    TaskListSerializer, TaskCreateSerializer, TaskAssignSerializer,
    TaskCreateAssignSerializer, TaskMasterSerializer, TaskMasterWriteSerializer,
    TaskAttachmentSerializer
)
from ..activity import ActivityLog, log_activity
from ..notification_utils import send_notification
from django.db import transaction
import math
from .utils import (
    _is_admin, _current_employee, _is_tl, _can_manage_tasks,
    _owns_task_for_management, task_list_queryset, _current_task_actor
)


def parse_due_time(time_str):
    if not time_str:
        return None
    import datetime
    if isinstance(time_str, datetime.time):
        return time_str
    time_str = str(time_str).strip()
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M%p", "%I:%M:%S %p", "%I:%M:%S%p"):
        try:
            return datetime.datetime.strptime(time_str, fmt).time()
        except ValueError:
            pass
    return None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_task(request):
    if not _is_admin(request):
        return Response({"detail": "Only admins can create tasks."}, status=status.HTTP_403_FORBIDDEN)

    serializer = TaskCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    task = serializer.save(assigned_by_admin=request.user.instance, created_by_admin=request.user.instance)
    return Response(TaskListSerializer(task).data, status=status.HTTP_201_CREATED)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def assign_task(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if not _is_admin(request):
        return Response({"detail": "Only admins can assign tasks."}, status=status.HTTP_403_FORBIDDEN)

    serializer = TaskAssignSerializer(task, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)

    new_employee = serializer.validated_data.get("assigned_to")
    new_admin = serializer.validated_data.get("assigned_to_admin")

    # Check if the assignee was actually changed to someone else
    assignee_changed = (
        ("assigned_to" in serializer.validated_data and new_employee != task.assigned_to) or
        ("assigned_to_admin" in serializer.validated_data and new_admin != task.assigned_to_admin)
    )

    with transaction.atomic():
        if assignee_changed:
            # If reassigned to a different person, close any running timer first
            open_session = task.sessions.filter(end_time__isnull=True).first()
            if open_session:
                open_session.close()
                task.recalc_total_time()
            task = serializer.save(task_status=Task.Status.NOT_STARTED)

            sender = request.user.instance.name if hasattr(request.user, "instance") and request.user.instance else "Management"
            assignee = task.assigned_to if task.assigned_to else task.assigned_to_admin
            url = "/employee/all_tasks" if task.assigned_to else "/admin/my_tasks"
            send_notification(
                recipient=assignee,
                title="Task Reassigned to You",
                message=f"{sender} assigned you task: '{task.task_name}' (Priority: {task.priority.capitalize()}).",
                event_type=Notification.EventType.TASK_ASSIGNED,
                action_url=url,
                sender_name=sender,
            )
        else:
            # If only details (due date, priority, hours) changed, PRESERVE current task_status
            task = serializer.save()

    return Response(TaskListSerializer(task).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_and_assign_task(request):
    if not _is_admin(request):
        return Response({"detail": "Only admins can create tasks."}, status=status.HTTP_403_FORBIDDEN)

    serializer = TaskCreateAssignSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    with transaction.atomic():
        task = serializer.save(
            assigned_by_admin=request.user.instance,
            created_by_admin=request.user.instance,
            task_status=Task.Status.NOT_STARTED,
        )

        log_activity(
            task, request.user, ActivityLog.Action.CREATED,
            to_status="not_started",
            details={
                "assigned_to": task.assignee_name,
                "assignee_role": task.assignee_role,
                "priority": task.priority,
                "due_date": str(task.due_date) if task.due_date else None,
                "allotted_time": str(task.allotted_time) if task.allotted_time is not None else None,
            },
        )

        sender = request.user.instance.name if hasattr(request.user, "instance") and request.user.instance else "Management"
        assignee = task.assigned_to if task.assigned_to else task.assigned_to_admin
        url = "/employee/all_tasks" if task.assigned_to else "/admin/my_tasks"
        send_notification(
            recipient=assignee,
            title="New Task Assigned",
            message=f"{sender} assigned you a new task: '{task.task_name}' (Priority: {task.priority.capitalize()}).",
            event_type=Notification.EventType.TASK_ASSIGNED,
            action_url=url,
            sender_name=sender,
        )

    return Response(TaskListSerializer(task).data, status=status.HTTP_201_CREATED)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_all_departments(request):
    """GET /api/tasks/get_all_departments/"""
    from accounts.views import _get_all_active_departments
    all_depts = _get_all_active_departments()
    return Response([{"id": name, "name": name} for name in all_depts])


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_departments_overview(request):
    """
    GET /api/tasks/departments/overview/
    Returns all departments with statistics for Trello-style cards.
    """
    from ..models import get_department_prefix
    from accounts.views import _get_all_active_departments
    all_depts = _get_all_active_departments()

    data = []
    for dept in all_depts:
        employees_qs = Employee.objects.filter(department=dept, is_active=True)
        tl_names = list(employees_qs.filter(role__in=["Team Lead", "TL"]).values_list("name", flat=True))
        tasks_qs = Task.objects.filter(department=dept)
        total_tasks = tasks_qs.count()

        projects = list(
            tasks_qs.exclude(project_name="")
            .values_list("project_name", flat=True)
            .distinct()
            .order_by("project_name")
        )
        completed = tasks_qs.filter(task_status=Task.Status.COMPLETED).count()
        review = tasks_qs.filter(task_status=Task.Status.UNDER_REVIEW).count()
        rework = tasks_qs.filter(task_status=Task.Status.REWORK_NEEDED).count()
        open_count = tasks_qs.filter(task_status__in=[
            Task.Status.NOT_STARTED,
            Task.Status.IN_PROGRESS,
            Task.Status.PAUSED,
            Task.Status.ON_HOLD,
            Task.Status.SUBMITTED,
            Task.Status.RESUBMITTED
        ]).count()

        completion_rate = round((completed / total_tasks * 100), 1) if total_tasks > 0 else 0

        # Normal tasks vs Recurring tasks breakdown
        normal_qs = tasks_qs.filter(recurring_source__isnull=True)
        recurring_qs = tasks_qs.filter(recurring_source__isnull=False)

        normal_projects = list(normal_qs.exclude(project_name="").values_list("project_name", flat=True).distinct())
        recurring_projects = list(recurring_qs.exclude(project_name="").values_list("project_name", flat=True).distinct())

        normal_completed = normal_qs.filter(task_status=Task.Status.COMPLETED).count()
        normal_total = normal_qs.count()
        normal_completion_rate = round((normal_completed / normal_total * 100), 1) if normal_total > 0 else 0

        rec_completed = recurring_qs.filter(task_status=Task.Status.COMPLETED).count()
        rec_total = recurring_qs.count()
        rec_completion_rate = round((rec_completed / rec_total * 100), 1) if rec_total > 0 else 0

        data.append({
            "name": dept,
            "prefix": get_department_prefix(dept),
            "members_count": employees_qs.count(),
            "team_leads": tl_names,
            "projects_count": len(projects),
            "projects": projects,
            "total_tasks": total_tasks,
            "open_tasks": open_count,
            "completed_tasks": completed,
            "review_tasks": review,
            "rework_tasks": rework,
            "completion_rate": completion_rate,
            # Separate Box Metrics:
            "normal_tasks_count": normal_total,
            "normal_projects_count": len(normal_projects),
            "normal_projects": normal_projects,
            "normal_completion_rate": normal_completion_rate,
            "recurring_tasks_count": rec_total,
            "recurring_projects_count": len(recurring_projects),
            "recurring_projects": recurring_projects,
            "recurring_completion_rate": rec_completion_rate,
        })
    return Response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_department_projects(request):
    """
    GET /api/tasks/departments/projects/?department=...&mode=normal|recurring|all
    Returns project cards within a specific department.
    """
    dept = request.query_params.get("department", "").strip()
    mode = request.query_params.get("mode", "all").strip().lower()
    if not dept:
        return Response({"detail": "Department parameter is required."}, status=status.HTTP_400_BAD_REQUEST)

    tasks_qs = Task.objects.filter(department=dept)
    if mode == "normal":
        tasks_qs = tasks_qs.filter(recurring_source__isnull=True)
    elif mode == "recurring":
        tasks_qs = tasks_qs.filter(recurring_source__isnull=False)

    project_names = list(
        tasks_qs.exclude(project_name="")
        .values_list("project_name", flat=True)
        .distinct()
        .order_by("project_name")
    )

    has_unassigned = tasks_qs.filter(project_name="").exists()
    all_projects = list(project_names)
    if has_unassigned:
        all_projects.append("General Tasks")

    projects_data = []
    for proj in all_projects:
        if proj == "General Tasks":
            p_tasks = tasks_qs.filter(project_name="")
        else:
            p_tasks = tasks_qs.filter(project_name=proj)

        total_p = p_tasks.count()
        completed_p = p_tasks.filter(task_status=Task.Status.COMPLETED).count()
        review_p = p_tasks.filter(task_status=Task.Status.UNDER_REVIEW).count()
        rework_p = p_tasks.filter(task_status=Task.Status.REWORK_NEEDED).count()
        open_p = p_tasks.filter(task_status__in=[
            Task.Status.NOT_STARTED,
            Task.Status.IN_PROGRESS,
            Task.Status.PAUSED,
            Task.Status.ON_HOLD,
            Task.Status.SUBMITTED,
            Task.Status.RESUBMITTED
        ]).count()

        assignee_names = list(
            p_tasks.exclude(assigned_to__isnull=True)
            .values_list("assigned_to__name", flat=True)
            .distinct()
        )
        admin_assignees = list(
            p_tasks.exclude(assigned_to_admin__isnull=True)
            .values_list("assigned_to_admin__name", flat=True)
            .distinct()
        )
        all_assignees = sorted(list(set(assignee_names + admin_assignees)))

        completion_rate = round((completed_p / total_p * 100), 1) if total_p > 0 else 0

        projects_data.append({
            "project_name": proj,
            "department": dept,
            "total_tasks": total_p,
            "open_tasks": open_p,
            "completed_tasks": completed_p,
            "review_tasks": review_p,
            "rework_tasks": rework_p,
            "completion_rate": completion_rate,
            "assignees": all_assignees,
        })

    return Response(projects_data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_project_tasks(request):
    """
    GET /api/tasks/project_tasks/?project_name=...&department=...&mode=normal|recurring|all
    Returns full task items for a specific project.
    """
    project_name = request.query_params.get("project_name", "").strip()
    department = request.query_params.get("department", "").strip()
    mode = request.query_params.get("mode", "all").strip().lower()
    bucket = request.query_params.get("bucket", "all").strip().lower()

    if mode == "recurring":
        from .recurring import generate_recurring_tasks
        generate_recurring_tasks()

    qs = task_list_queryset()
    if not _is_admin(request):
        employee = _current_employee(request)
        if employee is None:
            return Response([], status=status.HTTP_200_OK)
        from django.db.models import F, Q
        if mode == "recurring":
            qs = qs.filter(Q(assigned_to=employee) | Q(created_by_employee=employee))
        else:
            qs = qs.filter(assigned_to=employee)
        qs = qs.order_by(
            F("due_date").asc(nulls_last=True), F("due_time").asc(nulls_last=True), "id"
        )
    else:
        qs = qs.order_by("-assigned_date", "-id")
    if department:
        qs = qs.filter(department=department)
    elif getattr(request.user, "role", None) == "employee" and getattr(request.user.instance, "department", None):
        qs = qs.filter(department=request.user.instance.department)
    if project_name:
        if project_name == "General Tasks":
            qs = qs.filter(project_name="")
        else:
            qs = qs.filter(project_name=project_name)

    if mode == "normal":
        qs = qs.filter(recurring_source__isnull=True)
    elif mode == "recurring":
        qs = qs.filter(recurring_source__isnull=False)

    if bucket == "active":
        qs = qs.exclude(task_status__in=[
            Task.Status.SUBMITTED,
            Task.Status.UNDER_REVIEW,
            Task.Status.RESUBMITTED,
            Task.Status.COMPLETED,
            Task.Status.CANCELLED,
        ])
    elif bucket == "submitted":
        if _is_admin(request):
            return Response({"detail": "Admins should use the review queue."}, status=status.HTTP_403_FORBIDDEN)
        qs = qs.filter(task_status__in=[
            Task.Status.SUBMITTED,
            Task.Status.UNDER_REVIEW,
            Task.Status.RESUBMITTED,
        ])
    elif bucket == "review":
        if not _is_admin(request):
            return Response({"detail": "Only admins can access the review queue."}, status=status.HTTP_403_FORBIDDEN)
        qs = qs.filter(task_status__in=[
            Task.Status.SUBMITTED,
            Task.Status.UNDER_REVIEW,
            Task.Status.RESUBMITTED,
        ])
    elif bucket == "completed":
        qs = qs.filter(task_status=Task.Status.COMPLETED)
    elif bucket == "backlog":
        qs = qs.filter(assigned_to__isnull=True, assigned_to_admin__isnull=True)

    return Response(TaskListSerializer(qs, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def quick_create_task(request):
    """
    POST /api/tasks/quick_create/
    Jira-style quick create supporting all fields:
    task_name, project_name, department, assigned_to, assigned_to_admin, priority, task_status,
    start_date, due_date, due_time, allotted_hours, allotted_minutes, allotted_time,
    reporter_type, reporter_id, reporter, task_details
    """
    from django.utils import timezone
    from accounts.models import Admin, Employee

    task_name = str(request.data.get("task_name") or "").strip()
    if not task_name:
        return Response({"detail": "Task name is required."}, status=status.HTTP_400_BAD_REQUEST)

    project_name = str(request.data.get("project_name") or "").strip()
    department = str(request.data.get("department") or "").strip()
    priority = request.data.get("priority") or "medium"
    status_val = request.data.get("task_status") or "not_started"
    assigned_to_id = request.data.get("assigned_to")
    assigned_to_admin_id = request.data.get("assigned_to_admin")
    start_date = request.data.get("start_date") or None
    if not start_date:
        start_date = timezone.localdate()
    due_date = request.data.get("due_date") or None
    due_time = parse_due_time(request.data.get("due_time"))
    task_details = str(request.data.get("task_details") or "").strip()

    # Calculate allotted_time from allotted_hours & allotted_minutes if provided
    allotted_time = 1.0
    try:
        if "allotted_hours" in request.data or "allotted_minutes" in request.data:
            h = float(request.data.get("allotted_hours") or 0)
            m = float(request.data.get("allotted_minutes") or 0)
            if h < 0 or m < 0 or m >= 60:
                return Response({"detail": "Enter non-negative hours and minutes from 0 to 59."}, status=status.HTTP_400_BAD_REQUEST)
            allotted_time = round(h + (m / 60.0), 2)
        elif "allotted_time" in request.data and request.data.get("allotted_time") is not None:
            allotted_time = float(request.data.get("allotted_time"))
    except (ValueError, TypeError, OverflowError):
        return Response({"detail": "Allotted time must be a valid number."}, status=status.HTTP_400_BAD_REQUEST)

    if not math.isfinite(allotted_time) or allotted_time < 0 or allotted_time > 999999.99:
        return Response({"detail": "Allotted time must be between 0 and 999999.99 hours."}, status=status.HTTP_400_BAD_REQUEST)

    # Assignee: if assigned_to_admin_id, target Admin; if assigned_to_id passed, use Employee
    emp = None
    target_admin = None
    if assigned_to_admin_id:
        target_admin = Admin.objects.filter(pk=assigned_to_admin_id).first()
    elif assigned_to_id:
        emp = Employee.objects.filter(pk=assigned_to_id).first()

    if emp and not department:
        department = emp.department

    # Reporter resolution (can pass reporter_type + reporter_id, or "reporter": "admin_1" / "tl_3")
    reporter_type = request.data.get("reporter_type")
    reporter_id = request.data.get("reporter_id")
    reporter_raw = request.data.get("reporter")
    if reporter_raw and not reporter_type:
        raw_str = str(reporter_raw)
        if raw_str.startswith("admin_"):
            reporter_type = "admin"
            try:
                reporter_id = int(raw_str.replace("admin_", ""))
            except ValueError:
                pass
        elif raw_str.startswith("tl_") or raw_str.startswith("emp_"):
            reporter_type = "tl"
            try:
                reporter_id = int(raw_str.replace("tl_", "").replace("emp_", ""))
            except ValueError:
                pass

    assigned_by_admin = None
    assigned_by_employee = None

    if reporter_type == "admin" and reporter_id:
        assigned_by_admin = Admin.objects.filter(pk=reporter_id).first()
    elif reporter_type in ["tl", "employee"] and reporter_id:
        assigned_by_employee = Employee.objects.filter(pk=reporter_id).first()
    else:
        # Defaults
        if _is_admin(request):
            assigned_by_admin = request.user.instance
        else:
            current_emp = request.user.instance
            if current_emp and getattr(current_emp, "belong_to_admin", None):
                assigned_by_admin = current_emp.belong_to_admin
            elif current_emp and getattr(current_emp, "belong_to_tl", None):
                assigned_by_employee = current_emp.belong_to_tl
            else:
                assigned_by_admin = Admin.objects.first()

    task = Task.objects.create(
        task_name=task_name,
        task_details=task_details,
        project_name=project_name,
        department=department,
        priority=priority,
        task_status=status_val,
        assigned_to=emp,
        assigned_to_admin=target_admin,
        assigned_by_admin=assigned_by_admin,
        assigned_by_employee=assigned_by_employee,
        created_by_admin=request.user.instance if _is_admin(request) else None,
        created_by_employee=None if _is_admin(request) else request.user.instance,
        start_date=start_date,
        assigned_date=timezone.localdate(),
        due_date=due_date,
        due_time=due_time,
        allotted_time=allotted_time,
    )

    return Response(TaskListSerializer(task).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_all_employees(request):
    """GET /api/tasks/get_all_employees/?department=Engineering"""
    employees = Employee.objects.all()
    department = request.query_params.get("department")
    if department:
        employees = employees.filter(department=department)
    data = [{"id": e.id, "name": e.name, "department": e.department} for e in employees]
    return Response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_priority_choices(request):
    """GET /api/tasks/get_priority_choices/"""
    return Response([{"value": value, "label": label} for value, label in Task.Priority.choices])

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_all_admins(request):
    """GET /api/tasks/get_all_admins/ — dropdown source for admins."""
    from accounts.models import Admin
    admins = Admin.objects.filter(is_active=True).order_by("name")
    return Response([{"id": a.id, "name": a.name} for a in admins])

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_all_reporters(request):
    """
    GET /api/tasks/get_all_reporters/
    Returns all valid reporters (Admins).
    """
    from accounts.models import Admin
    admins = Admin.objects.filter(is_active=True).order_by("name")

    reporters = []
    for a in admins:
        reporters.append({
            "id": a.id,
            "name": a.name,
            "type": "admin",
            "role": "Administrator",
            "label": f"{a.name} (Admin)",
            "key": f"admin_{a.id}",
        })
    return Response(reporters)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_admin_my_tasks(request):
    """GET /api/tasks/admin_my_tasks/ — tasks a TL assigned TO this logged-in admin."""
    if not _is_admin(request):
        return Response({"detail": "Admins only."}, status=status.HTTP_403_FORBIDDEN)
    admin = request.user.instance
    tasks = task_list_queryset().filter(assigned_to_admin=admin)
    return Response(TaskListSerializer(tasks, many=True).data)

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def task_master_view(request):
    """GET /api/tasks/task_master/ and POST /api/tasks/task_master/"""
    if request.method == "GET":
        items = TaskMaster.objects.filter(is_active=True).order_by("project_name", "task_name")
        return Response(TaskMasterSerializer(items, many=True).data)

    if not _can_manage_tasks(request):
        return Response({"detail": "Only admins or team leads can manage task catalog."}, status=status.HTTP_403_FORBIDDEN)

    serializer = TaskMasterWriteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    obj = serializer.save()
    return Response(TaskMasterSerializer(obj).data, status=status.HTTP_201_CREATED)


@api_view(["DELETE", "PATCH"])
@permission_classes([IsAuthenticated])
def task_master_detail_view(request, pk):
    """DELETE /api/tasks/task_master/<int:pk>/"""
    if not _can_manage_tasks(request):
        return Response({"detail": "Only admins or team leads can manage task catalog."}, status=status.HTTP_403_FORBIDDEN)
    item = get_object_or_404(TaskMaster, pk=pk)
    if request.method == "DELETE":
        item.is_active = False
        item.save(update_fields=["is_active"])
        return Response(status=status.HTTP_204_NO_CONTENT)
    elif request.method == "PATCH":
        serializer = TaskMasterWriteSerializer(item, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        obj = serializer.save()
        return Response(TaskMasterSerializer(obj).data)


@api_view(["PATCH", "PUT"])
@permission_classes([IsAuthenticated])
def edit_task(request, pk):
    """
    PATCH /api/tasks/<int:pk>/edit/
    Full task edit endpoint.
    Prefilled with existing task data, allows changing any detail including assigned_to.
    Guarantees that task_id and sub_task_id remain identical.
    """
    try:
        task = get_object_or_404(Task, pk=pk)
        actor_kind, actor = _current_task_actor(request)
        is_creator = (
            actor_kind == "admin" and task.created_by_admin_id == actor.id
        ) or (
            actor_kind == "employee" and task.created_by_employee_id == actor.id
        )
        if not (_is_admin(request) or is_creator):
            return Response({"detail": "You do not have permission to edit this task."}, status=status.HTTP_403_FORBIDDEN)

        data = request.data

        # Store previous IDs to strictly ensure task_id and sub_task_id are NEVER changed
        original_task_id = task.task_id
        original_sub_task_id = task.sub_task_id

        # Handle assignee change
        assignee_changed = False
        if "assigned_to" in data:
            new_emp_id = data.get("assigned_to")
            target_emp = Employee.objects.filter(pk=new_emp_id).first() if new_emp_id else None
            if target_emp != task.assigned_to:
                assignee_changed = True
                task.assigned_to = target_emp
                task.assigned_to_admin = None
                if target_emp and target_emp.department and not data.get("department"):
                    task.department = target_emp.department

        if "assigned_to_admin" in data:
            new_admin_id = data.get("assigned_to_admin")
            from accounts.models import Admin
            target_admin = Admin.objects.filter(pk=new_admin_id).first() if new_admin_id else None
            if target_admin != task.assigned_to_admin:
                assignee_changed = True
                task.assigned_to_admin = target_admin
                task.assigned_to = None
                task.department = ""

        if "project_name" in data:
            task.project_name = str(data.get("project_name") or "").strip()
        if "task_status" in data and data.get("task_status"):
            task.task_status = data["task_status"]
        if "task_name" in data and data.get("task_name"):
            task.task_name = str(data["task_name"]).strip()
        if "task_details" in data:
            task.task_details = str(data.get("task_details") or "").strip()
        if "department" in data:
            dept_val = str(data.get("department") or "").strip()
            if dept_val:
                task.department = dept_val
        if "priority" in data and data.get("priority"):
            task.priority = data["priority"]
        if (_is_admin(request) or is_creator) and ("reporter" in data or "reporter_type" in data):
            reporter_raw = data.get("reporter")
            reporter_type = data.get("reporter_type")
            reporter_id = data.get("reporter_id")
            if reporter_raw and not reporter_type:
                raw_str = str(reporter_raw)
                if raw_str.startswith("admin_"):
                    reporter_type = "admin"
                    try:
                        reporter_id = int(raw_str.replace("admin_", ""))
                    except ValueError:
                        pass
                elif raw_str.startswith("tl_") or raw_str.startswith("emp_"):
                    reporter_type = "tl"
                    try:
                        reporter_id = int(raw_str.replace("tl_", "").replace("emp_", ""))
                    except ValueError:
                        pass

            from accounts.models import Admin
            if reporter_type == "admin" and reporter_id:
                task.assigned_by_admin = Admin.objects.filter(pk=reporter_id).first()
                task.assigned_by_employee = None
            elif reporter_type in ["tl", "employee"] and reporter_id:
                task.assigned_by_employee = Employee.objects.filter(pk=reporter_id).first()
                task.assigned_by_admin = None
        if "start_date" in data:
            task.start_date = data["start_date"] or None
        if "due_date" in data:
            task.due_date = data["due_date"] or None
        if "due_time" in data:
            task.due_time = parse_due_time(data["due_time"])
        if "allotted_time" in data and data.get("allotted_time") is not None:
            try:
                from decimal import Decimal
                task.allotted_time = Decimal(str(data["allotted_time"]))
            except (ValueError, TypeError, Exception):
                pass

        with transaction.atomic():
            if assignee_changed:
                open_session = task.sessions.filter(end_time__isnull=True).first()
                if open_session:
                    open_session.close()
                    task.recalc_total_time()
                if task.task_status != Task.Status.COMPLETED:
                    task.task_status = Task.Status.NOT_STARTED

                sender = request.user.instance.name if hasattr(request.user, "instance") and request.user.instance else "Management"
                assignee = task.assigned_to if task.assigned_to else task.assigned_to_admin
                if assignee:
                    url = "/employee/all_tasks" if task.assigned_to else "/admin/my_tasks"
                    try:
                        send_notification(
                            recipient=assignee,
                            title="Task Reassigned to You",
                            message=f"{sender} updated and assigned you task: '{task.task_name}' (Priority: {task.priority.capitalize()}).",
                            event_type=Notification.EventType.TASK_ASSIGNED,
                            action_url=url,
                            sender_name=sender,
                        )
                    except Exception as notif_err:
                        print(f"Failed to send task reassign notification: {notif_err}")

            # Strictly maintain same task_id and sub_task_id
            task.task_id = original_task_id
            task.sub_task_id = original_sub_task_id
            task.save()

            try:
                log_activity(
                    task, request.user,
                    ActivityLog.Action.ASSIGNED if assignee_changed else ActivityLog.Action.EDITED,
                    details={
                        "task_id": task.task_id,
                        "sub_task_id": task.sub_task_id,
                        "assigned_to": task.assignee_name,
                        "priority": task.priority,
                        "due_date": str(task.due_date) if task.due_date else None,
                    }
                )
            except Exception as log_err:
                print(f"Failed to log activity: {log_err}")

        return Response(TaskListSerializer(task).data, status=status.HTTP_200_OK)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


DISALLOWED_VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".3gp"
}

@api_view(["POST", "GET"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def task_attachments_view(request, pk):
    """POST/GET /api/tasks/<int:pk>/attachments/"""
    task = get_object_or_404(Task, pk=pk)
    if request.method == "POST":
        kind, actor = _current_task_actor(request)
        attachment_kwargs = {"uploaded_by": actor} if kind == "employee" else {"uploaded_by_admin": actor}
        files = request.FILES.getlist("attachments") or request.FILES.getlist("file") or request.FILES.getlist("attachment")
        if not files:
            return Response({"detail": "No file was uploaded."}, status=status.HTTP_400_BAD_REQUEST)

        import os
        import datetime
        from django.conf import settings
        from django.core.files.storage import FileSystemStorage
        from django.core.files.base import ContentFile
        from django.utils import timezone

        # Reject video uploads
        for f in files:
            ext = os.path.splitext(f.name)[1].lower()
            if ext in DISALLOWED_VIDEO_EXTENSIONS:
                return Response(
                    {"detail": f"Videos are not allowed ('{f.name}'). Only images, docx, ppt, pdf, and excel files are supported."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        local_fs = FileSystemStorage(location=settings.MEDIA_ROOT, base_url=settings.MEDIA_URL)
        created = []
        creation_meta_list = list(task.task_creation_attachments or [])
        try:
            for f in files:
                clean_name = os.path.basename(getattr(f, "name", "attachment"))
                now = datetime.datetime.now()

                # Read raw file bytes safely first
                if hasattr(f, "seek"):
                    try:
                        f.seek(0)
                    except Exception:
                        pass
                file_bytes = f.read()

                att = None
                # If S3 is enabled, attempt S3 save, but gracefully fallback if network/boto3 fails
                if getattr(settings, "USE_S3_STORAGE", False):
                    try:
                        cf_s3 = ContentFile(file_bytes, name=clean_name)
                        att = TaskAttachment.objects.create(task=task, file=cf_s3, is_creation_attachment=True, **attachment_kwargs)
                    except Exception as s3_err:
                        import logging
                        logging.getLogger(__name__).warning("S3 attachment save failed (%s); falling back to local storage.", s3_err)
                        att = None

                if att is None:
                    # Save locally in MEDIA_ROOT
                    subpath = f"task_attachments/{now.strftime('%Y/%m')}/{clean_name}"
                    cf_local = ContentFile(file_bytes, name=clean_name)
                    saved_rel_path = local_fs.save(subpath, cf_local)
                    att = TaskAttachment(task=task, is_creation_attachment=True, **attachment_kwargs)
                    att.file.name = saved_rel_path
                    att.file._committed = True
                    att.save()

                created.append(att)

                # Determine usable file url
                file_url = ""
                try:
                    local_path = os.path.join(settings.MEDIA_ROOT, att.file.name)
                    if os.path.exists(local_path):
                        file_url = f"{settings.MEDIA_URL}{att.file.name}"
                    else:
                        file_url = att.file.url
                except Exception:
                    file_url = f"{settings.MEDIA_URL}{att.file.name}"

                uploaded_at_iso = att.uploaded_at.isoformat() if att.uploaded_at else timezone.now().isoformat()
                creation_meta_list.append({
                    "id": att.id,
                    "name": clean_name,
                    "file": file_url,
                    "size": len(file_bytes) or getattr(f, "size", 0),
                    "uploaded_at": uploaded_at_iso,
                })
        except Exception as upload_err:
            import logging
            logging.getLogger(__name__).error("Attachment save failed: %s", upload_err)
            return Response({"detail": f"Failed to upload attachment: {str(upload_err)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        with transaction.atomic():
            task = Task.objects.select_for_update().get(pk=pk)
            existing_meta = list(task.task_creation_attachments or [])
            existing_ids = {m.get("id") for m in existing_meta if isinstance(m, dict)}
            for m in creation_meta_list:
                if m.get("id") not in existing_ids:
                    existing_meta.append(m)
                    existing_ids.add(m.get("id"))
            task.task_creation_attachments = existing_meta
            task.save(update_fields=["task_creation_attachments"])

        return Response(TaskAttachmentSerializer(task.attachments.all(), many=True).data, status=status.HTTP_201_CREATED)
    return Response(TaskAttachmentSerializer(task.attachments.all(), many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def download_task_attachment(request, pk, att_id):
    """GET /api/tasks/<int:pk>/attachments/<int:att_id>/download/"""
    from django.http import FileResponse, Http404
    import os
    import mimetypes
    from django.conf import settings

    att = get_object_or_404(TaskAttachment, pk=att_id, task_id=pk)
    if not att.file:
        raise Http404("File not found")

    clean_name = os.path.basename(att.file.name)
    content_type, _ = mimetypes.guess_type(clean_name)
    content_type = content_type or "application/octet-stream"

    local_path = os.path.join(settings.MEDIA_ROOT, att.file.name)
    if os.path.exists(local_path):
        return FileResponse(open(local_path, "rb"), as_attachment=True, filename=clean_name, content_type=content_type)
    else:
        try:
            return FileResponse(att.file.open("rb"), as_attachment=True, filename=clean_name, content_type=content_type)
        except Exception:
            from django.shortcuts import redirect
            return redirect(att.file.url)


@api_view(["POST", "GET"])
@permission_classes([IsAuthenticated])
def task_comments_view(request, pk):
    """POST/GET /api/tasks/<int:pk>/comments/"""
    task = get_object_or_404(Task, pk=pk)
    actor_kind, actor = _current_task_actor(request)
    can_view_comments = _is_admin(request) or (
        actor_kind == "employee" and (
            task.assigned_to_id == actor.id or task.created_by_employee_id == actor.id or task.assigned_by_employee_id == actor.id
        )
    ) or (
        actor_kind == "admin" and (
            task.assigned_to_admin_id == actor.id or task.created_by_admin_id == actor.id or task.assigned_by_admin_id == actor.id
        )
    )
    if not can_view_comments:
        return Response({"detail": "You do not have permission to view or comment on this task."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "POST":
        comment_text = str(request.data.get("comment") or "").strip()
        if not comment_text:
            return Response({"detail": "Comment cannot be empty."}, status=status.HTTP_400_BAD_REQUEST)
        log_activity(task, request.user, "comment", details={"comment": comment_text})

    logs = ActivityLog.objects.filter(task=task).order_by("-created_at")
    comments = []
    for l in logs:
        text = l.details.get("comment", "") if isinstance(l.details, dict) else ""
        if text or l.action == "comment":
            comments.append({
                "id": l.id,
                "actor": l.actor_name,
                "text": text,
                "created_at": l.created_at.isoformat(),
            })
    return Response(comments)
