from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.access import require_mailprint_admin
from core.models import AppConfiguration, AuditLog

from .forms import AcknowledgementTemplateForm, FilterRuleForm, schedule_form_for
from .models import AcknowledgementTemplate, FilterRule


def _require_admin(request):
    require_mailprint_admin(request.user)


def _configuration():
    configuration, _ = AppConfiguration.objects.get_or_create(key=1)
    return configuration


@login_required
def rule_list(request):
    _require_admin(request)
    configuration = _configuration()
    return render(
        request,
        "rules/list.html",
        {
            "rules": FilterRule.objects.all(),
            "configuration": configuration,
            "schedule_form": schedule_form_for(configuration),
        },
    )


@login_required
def rule_create(request):
    _require_admin(request)
    form = FilterRuleForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        rule = form.save()
        AuditLog.objects.create(
            user=request.user,
            action="rule_created",
            object_type="FilterRule",
            object_id=str(rule.pk),
        )
        messages.success(request, "Filtering rule created.")
        return redirect("rules:list")
    return render(
        request,
        "rules/form.html",
        {"form": form, "title": "Create rule", "submit_label": "Create rule"},
    )


@login_required
def rule_edit(request, rule_id):
    _require_admin(request)
    rule = get_object_or_404(FilterRule, pk=rule_id)
    form = FilterRuleForm(request.POST or None, instance=rule)
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditLog.objects.create(
            user=request.user,
            action="rule_updated",
            object_type="FilterRule",
            object_id=str(rule.pk),
        )
        messages.success(request, "Filtering rule updated.")
        return redirect("rules:list")
    return render(
        request,
        "rules/form.html",
        {"form": form, "title": f"Edit {rule.name}", "submit_label": "Save changes"},
    )


@login_required
@require_POST
def rule_delete(request, rule_id):
    _require_admin(request)
    rule = get_object_or_404(FilterRule, pk=rule_id)
    rule_name = rule.name
    rule_id_value = rule.pk
    rule.delete()
    AuditLog.objects.create(
        user=request.user,
        action="rule_deleted",
        object_type="FilterRule",
        object_id=str(rule_id_value),
        details={"name": rule_name},
    )
    messages.success(request, f"Rule {rule_name!r} deleted.")
    return redirect("rules:list")


@login_required
@require_POST
def rule_schedule(request):
    _require_admin(request)
    configuration = _configuration()
    form = schedule_form_for(configuration, data=request.POST)
    if not form.is_valid():
        messages.error(request, "Schedule was not saved; check the selected days and times.")
        return redirect("rules:list")

    configuration.schedule_enabled = form.cleaned_data["enabled"]
    configuration.default_action = form.cleaned_data["default_action"]
    configuration.schedule_weekdays = form.cleaned_data["weekdays"]
    configuration.schedule_start = form.cleaned_data["start"]
    configuration.schedule_end = form.cleaned_data["end"]
    configuration.save(
        update_fields=[
            "schedule_enabled",
            "default_action",
            "schedule_weekdays",
            "schedule_start",
            "schedule_end",
            "updated_at",
        ]
    )
    AuditLog.objects.create(
        user=request.user,
        action="auto_print_schedule_updated",
        object_type="AppConfiguration",
        object_id="1",
        details={
            "enabled": configuration.schedule_enabled,
            "default_action": configuration.default_action,
            "weekdays": configuration.schedule_weekdays,
            "start": configuration.schedule_start.isoformat() if configuration.schedule_start else "",
            "end": configuration.schedule_end.isoformat() if configuration.schedule_end else "",
        },
    )
    messages.success(request, "Auto-print schedule saved.")
    return redirect("rules:list")


@login_required
def template_list(request):
    _require_admin(request)
    return render(
        request,
        "rules/templates.html",
        {
            "templates": AcknowledgementTemplate.objects.all(),
            "configuration": _configuration(),
        },
    )


@login_required
def template_create(request):
    _require_admin(request)
    form = AcknowledgementTemplateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        template = form.save()
        AuditLog.objects.create(
            user=request.user,
            action="ack_template_created",
            object_type="AcknowledgementTemplate",
            object_id=str(template.pk),
        )
        messages.success(request, "Acknowledgement template created.")
        return redirect("rules:templates")
    return render(
        request,
        "rules/template_form.html",
        {"form": form, "title": "Create acknowledgement template"},
    )


@login_required
def template_edit(request, template_id):
    _require_admin(request)
    template = get_object_or_404(AcknowledgementTemplate, pk=template_id)
    form = AcknowledgementTemplateForm(request.POST or None, instance=template)
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditLog.objects.create(
            user=request.user,
            action="ack_template_updated",
            object_type="AcknowledgementTemplate",
            object_id=str(template.pk),
        )
        messages.success(request, "Acknowledgement template updated.")
        return redirect("rules:templates")
    return render(
        request,
        "rules/template_form.html",
        {"form": form, "title": f"Edit {template.name}"},
    )


@login_required
@require_POST
def template_delete(request, template_id):
    _require_admin(request)
    template = get_object_or_404(AcknowledgementTemplate, pk=template_id)
    if template.is_default and _configuration().auto_acknowledge_after_print:
        messages.error(
            request,
            "Disable automatic acknowledgements or choose a replacement default before deleting this template.",
        )
        return redirect("rules:templates")
    template_id_value = template.pk
    template_name = template.name
    template.delete()
    AuditLog.objects.create(
        user=request.user,
        action="ack_template_deleted",
        object_type="AcknowledgementTemplate",
        object_id=str(template_id_value),
        details={"name": template_name},
    )
    messages.success(request, f"Template {template_name!r} deleted.")
    return redirect("rules:templates")


@login_required
@require_POST
def toggle_auto_acknowledge(request):
    _require_admin(request)
    enabled = request.POST.get("enabled")
    if enabled not in ("true", "false"):
        messages.error(request, "Choose whether to enable automatic acknowledgements.")
        return redirect("rules:templates")
    if enabled == "true" and not AcknowledgementTemplate.objects.filter(
        enabled=True,
        is_default=True,
    ).exists():
        messages.error(
            request,
            "Choose an enabled default template before enabling automatic acknowledgements.",
        )
        return redirect("rules:templates")
    configuration = _configuration()
    configuration.auto_acknowledge_after_print = enabled == "true"
    configuration.save(
        update_fields=["auto_acknowledge_after_print", "updated_at"]
    )
    AuditLog.objects.create(
        user=request.user,
        action=(
            "auto_acknowledgement_enabled"
            if configuration.auto_acknowledge_after_print
            else "auto_acknowledgement_disabled"
        ),
        object_type="AppConfiguration",
        object_id="1",
        details={"enabled": configuration.auto_acknowledge_after_print},
    )
    messages.success(
        request,
        "Automatic acknowledgements are "
        + ("ON." if configuration.auto_acknowledge_after_print else "OFF."),
    )
    return redirect("rules:templates")
