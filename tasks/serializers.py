# tasks/serializers.py

from rest_framework import serializers
from .models import Task, TimerSession, TaskMaster, RecurringTaskDefinition, TaskAttachment, CorrectionRequest, Notification
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone


class TaskAttachmentSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    size = serializers.SerializerMethodField()
    file = serializers.SerializerMethodField()

    class Meta:
        model = TaskAttachment
        fields = ["id", "file", "name", "size", "uploaded_at", "uploaded_by", "uploaded_by_admin", "is_creation_attachment"]

    def get_name(self, obj):
        import os
        return os.path.basename(obj.file.name) if obj.file else "attachment"

    def get_size(self, obj):
        try:
            if not obj.file:
                return 0
            import os
            from django.conf import settings
            local_path = os.path.join(settings.MEDIA_ROOT, obj.file.name)
            if os.path.exists(local_path):
                return os.path.getsize(local_path)
            return obj.file.size
        except Exception:
            return 0

    def get_file(self, obj):
        if not obj.file:
            return ""
        import os
        from django.conf import settings
        local_path = os.path.join(settings.MEDIA_ROOT, obj.file.name)
        if os.path.exists(local_path):
            return f"{settings.MEDIA_URL}{obj.file.name}"
        try:
            return obj.file.url
        except Exception:
            return f"{settings.MEDIA_URL}{obj.file.name}"


class TaskListSerializer(serializers.ModelSerializer):
    """
    Used for the task table. assigned_to_name / department_name / assigned_by_name
    are derived — department isn't its own table, it's just Employee.department,
    so we read it off whichever employee the task is currently assigned to.
    Uses SerializerMethodField (not a dotted `source=`) so unassigned tasks
    (assigned_to = None) don't blow up with an AttributeError.

    assigned_to_name / assignee_role are now generic across the Employee OR
    Admin assignee — see Task.assignee_name / Task.assignee_role.
    """
    assigned_to_name = serializers.SerializerMethodField()
    assignee_role = serializers.SerializerMethodField()      # "employee" | "admin" | None
    created_by_role = serializers.SerializerMethodField()   # "admin" | "tl" — lets the UI badge TL-created tasks
    department_name = serializers.SerializerMethodField()
    assigned_by_name = serializers.SerializerMethodField()
    reporter_key = serializers.SerializerMethodField()
    has_active_session = serializers.SerializerMethodField()
    is_recurring = serializers.SerializerMethodField()
    attachments = TaskAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = Task
        fields = [
            "id", "task_id", "sub_task_id", "department", "project_name", "task_name", "task_details",
            "assigned_to", "assigned_to_admin", "assigned_to_name", "assignee_role", "department_name",
            "assigned_by_name", "assigned_by_admin", "assigned_by_employee", "reporter_key",
            "created_by_admin", "created_by_employee",
            "priority", "start_date", "assigned_date", "due_date", "due_time", "allotted_time",
            "task_status", "total_time_taken", "remaining_or_over_time",
            "task_sheet_link", "employee_remarks", "submitted_date",
            "quality_of_task", "rating", "admin_remarks", "reviewed_date",
            "rework_count", "has_active_session", "created_by_role", "attachments", "cancel_reason",
            "is_recurring", "recurring_source", "task_creation_attachments"
        ]

    def get_reporter_key(self, obj):
        if obj.assigned_by_admin_id:
            return f"admin_{obj.assigned_by_admin_id}"
        if obj.assigned_by_employee_id:
            return f"tl_{obj.assigned_by_employee_id}"
        return None

    def get_is_recurring(self, obj):
        return obj.recurring_source_id is not None

    def get_assigned_to_name(self, obj):
        return obj.assignee_name

    def get_assignee_role(self, obj):
        return obj.assignee_role

    def get_department_name(self, obj):
        # Admin tasks have no department
        if obj.assigned_to_admin_id:
            return None
        # Prefer task's explicit department if set, else fallback to assigned employee's department
        if obj.department:
            return obj.department
        return obj.assigned_to.department if obj.assigned_to_id else None

    def get_assigned_by_name(self, obj):
        return obj.assigned_by_name

    def get_created_by_role(self, obj):
        return obj.created_by_role

    def get_has_active_session(self, obj):
        # Falls back to a query only if the view forgot to use task_list_queryset()
        # (shouldn't happen once every view is updated, but keeps this serializer safe).
        if hasattr(obj, "_has_active_session"):
            return obj._has_active_session
        return obj.sessions.filter(end_time__isnull=True).exists()


class TaskCreateSerializer(serializers.ModelSerializer):
    """
    Step 1 of the two-step flow: just the name and details. assigned_by is
    set in the view from the logged-in admin, never from the request body.
    """
    class Meta:
        model = Task
        fields = ["project_name", "task_name", "task_details"]

    def validate_task_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Task name cannot be blank.")
        return value


class TaskAssignSerializer(serializers.ModelSerializer):
    """
    Step 2: assign / reassign. Now supports assigning to either an Employee
    (assigned_to) OR an Admin (assigned_to_admin) — exactly one must be set.
    Whichever wins clears the other FK so a reassignment never leaves a
    stale pointer behind.
    """
    class Meta:
        model = Task
        fields = ["assigned_to", "assigned_to_admin", "priority", "start_date", "due_date", "due_time", "allotted_time"]
        extra_kwargs = {
            "assigned_to": {"required": False},
            "assigned_to_admin": {"required": False},
            "priority": {"required": True},
            "due_date": {"required": True},
            "allotted_time": {"required": True},
        }

    def validate(self, attrs):
        assigned_to = attrs.get("assigned_to")
        assigned_to_admin = attrs.get("assigned_to_admin")
        if not assigned_to and not assigned_to_admin:
            raise serializers.ValidationError("Assign this task to either an employee or an admin.")
        if assigned_to and assigned_to_admin:
            raise serializers.ValidationError("Assign to either an employee or an admin, not both.")
        return attrs

    def save(self, **kwargs):
        if self.validated_data.get("assigned_to_admin"):
            self.validated_data["assigned_to"] = None
            if hasattr(self, "instance") and self.instance:
                self.instance.department = ""
        elif self.validated_data.get("assigned_to"):
            self.validated_data["assigned_to_admin"] = None
        return super().save(**kwargs)


class TimerSessionSerializer(serializers.ModelSerializer):
    """Read-only — used to show session history for a task."""
    class Meta:
        model = TimerSession
        fields = ["id", "start_time", "end_time", "duration_seconds", "is_rework_session"]


class TaskSubmitSerializer(serializers.Serializer):
    """
    Body for POST /api/tasks/<id>/submit/. task_sheet_link is stored as
    entered, no URL validation, per the flowchart's confirmed constraints.
    """
    task_sheet_link = serializers.CharField(required=True, allow_blank=False)
    employee_remarks = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_task_sheet_link(self, value):
        value = value.strip()
        validator = URLValidator(schemes=["http", "https"])
        try:
            validator(value)
        except DjangoValidationError:
            raise serializers.ValidationError("Enter a valid http:// or https:// URL.")
        return value


class ReviewApproveSerializer(serializers.Serializer):
    quality_of_task = serializers.ChoiceField(choices=Task.Quality.choices)
    rating = serializers.IntegerField(min_value=1, max_value=5)
    admin_remarks = serializers.CharField(required=False, allow_blank=True, default="")


class ReviewReworkSerializer(serializers.Serializer):
    admin_remarks = serializers.CharField(required=True, allow_blank=False)


class CorrectionListSerializer(serializers.ModelSerializer):
    """Used by the pending/approved/rejected correction list screens."""
    task_id = serializers.CharField(source="session.task.task_id", read_only=True)
    task_name = serializers.CharField(source="session.task.task_name", read_only=True)
    employee_name = serializers.CharField(source="requested_by.name", read_only=True)
    decided_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CorrectionRequest
        fields = [
            "id", "task_id", "task_name", "employee_name", "reason",
            "original_end_time", "requested_end_time", "status",
            "admin_notes", "decided_by_name", "decided_at", "created_at",
        ]

    def get_decided_by_name(self, obj):
        return obj.decided_by.name if obj.decided_by_id else None


class TaskCreateAssignSerializer(serializers.ModelSerializer):
    """
    Combined create+assign in one shot. Same required fields as
    TaskAssignSerializer, plus task_name/task_details from TaskCreateSerializer.
    Used by POST /api/tasks/create_and_assign/. Supports assigning to either
    an Employee or an Admin — see TaskAssignSerializer's validate()/save().
    """
    class Meta:
        model = Task
        fields = [
            "project_name", "task_name", "task_details", "department",
            "assigned_to", "assigned_to_admin", "priority", "start_date", "due_date", "due_time", "allotted_time",
        ]
        extra_kwargs = {
            "assigned_to": {"required": False},
            "assigned_to_admin": {"required": False},
            "priority": {"required": True},
            "start_date": {"required": False},
            "due_date": {"required": True},
            "allotted_time": {"required": True},
        }

    def to_internal_value(self, data):
        mutable_data = data.copy() if hasattr(data, "copy") else dict(data)
        if ("allotted_hours" in mutable_data or "allotted_minutes" in mutable_data) and "allotted_time" not in mutable_data:
            h = float(mutable_data.get("allotted_hours") or 0)
            m = float(mutable_data.get("allotted_minutes") or 0)
            mutable_data["allotted_time"] = round(h + (m / 60.0), 2)
        if "due_time" in mutable_data and mutable_data["due_time"]:
            from .views.task_views import parse_due_time
            parsed_time = parse_due_time(mutable_data["due_time"])
            if parsed_time:
                mutable_data["due_time"] = parsed_time
        return super().to_internal_value(mutable_data)

    def validate_task_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Task name cannot be blank.")
        return value

    def validate(self, attrs):
        assigned_to = attrs.get("assigned_to")
        assigned_to_admin = attrs.get("assigned_to_admin")
        department = attrs.get("department")
        if not assigned_to and not assigned_to_admin:
            raise serializers.ValidationError("Assign this task to either an employee or an admin.")
        if assigned_to and assigned_to_admin:
            raise serializers.ValidationError("Assign to either an employee or an admin, not both.")
        if assigned_to and department:
            if assigned_to.department and assigned_to.department.strip().lower() != department.strip().lower():
                raise serializers.ValidationError(
                    f"Employee '{assigned_to.name}' belongs to department '{assigned_to.department}', not '{department}'."
                )
        if assigned_to_admin:
            attrs["department"] = ""
        return attrs

    def save(self, **kwargs):
        if self.validated_data.get("assigned_to_admin"):
            self.validated_data["assigned_to"] = None
            self.validated_data["department"] = ""
        elif self.validated_data.get("assigned_to"):
            self.validated_data["assigned_to_admin"] = None
        return super().save(**kwargs)


class TaskMasterSerializer(serializers.ModelSerializer):
    label = serializers.SerializerMethodField()

    class Meta:
        model = TaskMaster
        fields = ["id", "project_name", "task_name", "default_hours", "is_active", "label"]

    def get_label(self, obj):
        total_minutes = round(float(obj.default_hours) * 60)
        hours, minutes = divmod(total_minutes, 60)
        if hours and minutes:
            duration = f"{hours}h {minutes}m"
        elif hours:
            duration = f"{hours}h"
        else:
            duration = f"{minutes}m"
        return f"{obj.task_name} - {duration}"


class TaskMasterWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskMaster
        fields = ["id", "project_name", "task_name", "default_hours", "is_active"]
        extra_kwargs = {
            "is_active": {"required": False},
        }

    def validate_project_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Project name cannot be blank.")
        return value

    def validate_task_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Task name cannot be blank.")
        return value

    def validate_default_hours(self, value):
        if value <= 0:
            raise serializers.ValidationError("Hours must be greater than 0.")
        return value

    def validate(self, attrs):
        name = attrs.get("task_name", getattr(self.instance, "task_name", None))
        hours = attrs.get("default_hours", getattr(self.instance, "default_hours", None))
        qs = TaskMaster.objects.filter(task_name__iexact=name, default_hours=hours)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                {"default_hours": f'"{name}" with {hours}hr already exists in the catalog.'}
            )
        return attrs


class RecurringTaskDefinitionSerializer(serializers.ModelSerializer):
    """Read view — used by the admin's recurring-tasks management screen."""
    assigned_to_name = serializers.SerializerMethodField()
    assignee_role = serializers.SerializerMethodField()   # NEW — "employee" | "admin" | None

    class Meta:
        model = RecurringTaskDefinition
        fields = [
            "id", "project_name", "department", "task_name", "task_details",
            "assigned_to", "assigned_to_admin", "assigned_to_name", "assignee_role",  # CHANGED
            "priority", "allotted_time", "frequency", "start_date", "end_date", "weekdays",
            "excluded_dates",  # <-- ADD THIS
            "is_active", "created_at",
        ]

    def get_assigned_to_name(self, obj):
        return obj.assignee_name   # CHANGED — generic, not obj.assigned_to.name

    def get_assignee_role(self, obj):
        return obj.assignee_role


class RecurringTaskDefinitionCreateSerializer(serializers.ModelSerializer):
    """Write view — used by the Create Task modal when 'Repeats Daily' is checked."""
    weekdays = serializers.ListField(
        child=serializers.IntegerField(min_value=0, max_value=6),
        required=True,
    )

    class Meta:
        model = RecurringTaskDefinition
        fields = [
            "project_name", "department", "task_name", "task_details", "assigned_to", "assigned_to_admin",  # CHANGED
            "priority", "allotted_time", "start_date", "end_date", "weekdays",
        ]
        extra_kwargs = {
            "project_name": {"required": False},   # optional, matches Task.project_name being blank=True
            "department": {"required": False},
            "assigned_to": {"required": False},
            "assigned_to_admin": {"required": False},   # NEW
            "priority": {"required": True},
            "allotted_time": {"required": True},
            "start_date": {"required": True},
        }

    def validate_task_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Task name cannot be blank.")
        return value

    def validate(self, attrs):
        end_date = attrs.get("end_date", getattr(self.instance, "end_date", None))
        start_date = attrs.get("start_date", getattr(self.instance, "start_date", None))
        if "start_date" in attrs and start_date < timezone.localdate():
            raise serializers.ValidationError({"start_date": "Start date cannot be before today."})
        if end_date and start_date and end_date < start_date:
            raise serializers.ValidationError({"end_date": "End date can't be before start date."})

        # Recurring rules may be unassigned until the admin fills in the schedule.
        assigned_to = attrs.get("assigned_to")
        assigned_to_admin = attrs.get("assigned_to_admin")
        if assigned_to and assigned_to_admin:
            raise serializers.ValidationError("Assign to either an employee or an admin, not both.")
        return attrs

    def save(self, **kwargs):
        # NEW — whichever wins clears the other FK
        if self.validated_data.get("assigned_to_admin"):
            self.validated_data["assigned_to"] = None
        elif self.validated_data.get("assigned_to"):
            self.validated_data["assigned_to_admin"] = None
        return super().save(**kwargs)


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            "id", "sender_name", "title", "message", "event_type",
            "action_url", "is_read", "created_at"
        ]
