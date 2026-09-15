"""Admin classes and registrations for chat application."""

import logging

from django import forms
from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db.models import BigIntegerField, Exists, F, Func, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, render
from django.template.defaultfilters import filesizeformat
from django.urls import path, reverse
from django.utils.html import format_html

import httpx

from . import models
from .arena_results import build_results
from .model_health import set_model_health

logger = logging.getLogger(__name__)


def _stored_size(field_name):
    """
    Bytes this column occupies on disk, as stored.

    `pg_column_size` reads the size out of the tuple (or, for an out-of-line value, out
    of its TOAST pointer) instead of fetching and decompressing the value, so it stays
    cheap on the very rows we are looking for. The counterpart is that it reports the
    *compressed* size: JSON text compresses several times over, so the number is a
    ranking signal, not the payload size an API response would carry.
    """
    return Coalesce(
        Func(F(field_name), function="pg_column_size", output_field=BigIntegerField()),
        Value(0),
    )


class ChatConversationAttachmentInline(admin.TabularInline):
    """
    Files attached to a conversation, listed on the conversation change page.

    Read-only on purpose: an attachment also owns an object in the storage bucket and,
    once indexed, a document in the RAG collection. That cleanup lives in the API
    viewset's `perform_destroy`, not in a model signal, so adding or deleting rows here
    would leave the bucket and the collection out of sync with the database.
    """

    model = models.ChatConversationAttachment
    extra = 0
    can_delete = False
    show_change_link = True
    fields = (
        "file_name",
        "content_type",
        "size",
        "upload_state",
        # `is_indexed`, not `index_state`: only the project flow writes
        # `index_state`, so on a conversation attachment it is always left at its
        # default and would report every row here as not indexed.
        "is_indexed",
        "rag_document_id",
        "conversion_from",
        "processing_error",
        "uploaded_by",
        "created_at",
    )
    readonly_fields = fields

    def get_queryset(self, request):
        """`uploaded_by` is rendered on every row, so fetch it in the same query."""
        return super().get_queryset(request).select_related("uploaded_by")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(models.ChatConversation)
class ChatConversationAdmin(admin.ModelAdmin):
    """Admin class for the ChatConversation model"""

    search_fields = ("id", "title", "owner__email", "owner__sub", "project__title")
    ordering = ("-updated_at",)
    # Sized for bulk selection: the page costs ~0.25ms of template rendering per row
    # and a flat 2 queries, since the blobs are deferred and the COUNT(*) is off below.
    # 200 keeps the render under ~75ms while quadrupling how many rows can be deleted
    # in one pass; beyond that the browser, not the backend, becomes the bottleneck.
    list_per_page = 200
    # Skip the extra unfiltered COUNT(*) over the whole table on every changelist load.
    show_full_result_count = False

    autocomplete_fields = ("owner", "project")
    list_select_related = ("owner", "project")
    list_filter = (
        ("created_at", admin.DateFieldListFilter),
        ("project", admin.EmptyFieldListFilter),
        ("collection_id", admin.EmptyFieldListFilter),
    )

    list_display = (
        "id",
        "title",
        "owner",
        "project",
        "has_files",
        "stored_size",
        "collection_id",
        "created_at",
        "updated_at",
    )
    inlines = (ChatConversationAttachmentInline,)

    def get_queryset(self, request):
        """
        Leave the large text payloads out of the query.

        A conversation carries its whole message history in `messages`,
        `pydantic_messages` and `ui_messages`, plus its generated recap in
        `history_summary`. Selecting them means reading (and, for `messages`,
        running Pydantic validation on) megabytes of JSON per page while the
        changelist displays none of it. The change form still loads them lazily,
        one query per field, for the single object it edits.

        `has_files` is an EXISTS subquery rather than a COUNT join: a
        `Count("attachments")` would need a GROUP BY over the whole table before the
        page's LIMIT could apply, while EXISTS is evaluated per returned row against
        the attachment foreign key index.

        `stored_size` sums the deferred columns without selecting them, so heavy
        conversations can be sorted to the top of the page they are hiding in.
        """
        return (
            super()
            .get_queryset(request)
            .defer(
                "ui_messages",
                "pydantic_messages",
                "messages",
                "agent_usage",
                "history_summary",
            )
            .annotate(
                has_files=Exists(
                    models.ChatConversationAttachment.objects.filter(conversation=OuterRef("pk"))
                ),
                stored_size=(
                    _stored_size("messages")
                    + _stored_size("pydantic_messages")
                    + _stored_size("ui_messages")
                    + _stored_size("agent_usage")
                    + _stored_size("history_summary")
                ),
            )
        )

    @admin.display(description="Files", boolean=True)
    def has_files(self, obj):
        """Whether the conversation has attachments, without opening it."""
        return obj.has_files

    @admin.display(description="Stored size", ordering="stored_size")
    def stored_size(self, obj):
        """Compressed weight of the conversation payloads, sortable to find the outliers."""
        return filesizeformat(obj.stored_size)


@admin.register(models.ChatConversationAttachment)
class ChatConversationAttachmentAdmin(admin.ModelAdmin):
    """Admin class for the ChatConversationAttachment model"""

    search_fields = (
        "id",
        "conversation__id",
        "project__id",
        "file_name",
        "key",
        "rag_document_id",
        "uploaded_by__email",
    )
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50
    show_full_result_count = False
    readonly_fields = ("content_type", "upload_state", "size")
    list_display = (
        "id",
        "file_name",
        "scope",
        "content_type",
        "upload_state",
        "size",
        "conversation",
        "project",
        "uploaded_by",
        "rag_document_id",
        "conversion_from",
        "created_at",
        "updated_at",
    )
    autocomplete_fields = ("conversation", "project", "uploaded_by")
    list_filter = (
        "content_type",
        ("rag_document_id", admin.EmptyFieldListFilter),
        ("conversion_from", admin.EmptyFieldListFilter),
    )
    list_select_related = (
        "conversation",
        "project",
        "uploaded_by",
    )

    @admin.display(description="Scope")
    def scope(self, obj):
        """Show whether the attachment is owned by a conversation or a project."""
        if obj.conversation_id:
            return "conversation"
        if obj.project_id:
            return "project"
        return "-"


@admin.register(models.ModelHealth)
class ModelHealthAdmin(admin.ModelAdmin):
    """Read-only admin showing the latest health status per (provider, model)."""

    list_display = ("provider", "model_id", "status", "created_at", "updated_at")
    list_filter = ("provider", "status")
    readonly_fields = ("provider", "model_id", "created_at", "updated_at")

    def get_queryset(self, request):
        latest_id = (
            models.ModelHealth.objects.filter(
                provider=OuterRef("provider"), model_id=OuterRef("model_id")
            )
            .order_by("-updated_at")
            .values("id")[:1]
        )
        return super().get_queryset(request).filter(id=Subquery(latest_id))

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        """Persist the record and mirror the new status into the cache that readers use."""
        super().save_model(request, obj, form, change)
        set_model_health(obj.provider, obj.model_id, obj.status)


@admin.register(models.ChatProject)
class ChatProjectAdmin(admin.ModelAdmin):
    """Admin class for the ChatProject model"""

    search_fields = ("id", "title")
    ordering = ("-updated_at",)
    date_hierarchy = "created_at"
    list_filter = ("color", "icon")
    autocomplete_fields = ("owner",)
    list_select_related = ("owner",)
    list_display = (
        "id",
        "title",
        "owner",
        "collection_id",
        "icon",
        "color",
        "created_at",
        "updated_at",
    )


# --------------------------------------------------------------------------- #
# Arena
# --------------------------------------------------------------------------- #

ALBERT_MODEL_LIST_TIMEOUT_SECONDS = 3
NOT_CONFIGURED_GROUP = "Available on the provider, not configured (add to the LLM configuration)"


def _configured_model_choices() -> list[tuple[str, str]]:
    """Active models of the LLM configuration, as select choices."""
    return [
        (hrid, f"{model.human_readable_name} ({hrid})")
        for hrid, model in settings.LLM_CONFIGURATIONS.items()
        if model.is_active
    ]


def _provider_model_ids() -> list[str]:
    """Model ids the default model's provider advertises, or [] when unreachable.

    The provider is OpenAI compatible, so ``GET {base_url}/models`` lists them.
    Purely informative: the app can only run models present in its configuration.
    """
    default = settings.LLM_CONFIGURATIONS.get(settings.LLM_DEFAULT_MODEL_HRID)
    provider = getattr(default, "provider", None)
    if provider is None or not provider.base_url:
        return []
    url = f"{str(provider.base_url).rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}
    try:
        response = httpx.get(url, headers=headers, timeout=ALBERT_MODEL_LIST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json().get("data", [])
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("Could not list provider models from %s: %s", url, exc)
        return []
    return sorted(str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id"))


class DisablingSelect(forms.Select):
    """Select widget rendering the given values as disabled (greyed) options."""

    def __init__(self, *args, disabled_values=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.disabled_values = set(disabled_values)

    def create_option(self, *args, **kwargs):  # pylint: disable=arguments-differ
        option = super().create_option(*args, **kwargs)
        if option["value"] in self.disabled_values:
            option["attrs"]["disabled"] = True
        return option


def _model_choice_field(label: str, with_provider_list: bool):
    """A model dropdown: configured active models, then greyed provider-only ones."""
    configured = _configured_model_choices()
    configured_names = {str(model.model_name) for model in settings.LLM_CONFIGURATIONS.values()}
    choices: list = list(configured)
    disabled: list[str] = []
    if with_provider_list:
        extra = [
            (f"__unconfigured__{model_id}", model_id)
            for model_id in _provider_model_ids()
            if model_id not in configured_names
        ]
        if extra:
            choices.append((NOT_CONFIGURED_GROUP, extra))
            disabled = [value for value, _ in extra]
    return forms.ChoiceField(
        label=label,
        choices=choices,
        widget=DisablingSelect(disabled_values=disabled),
    )


class ArenaExperimentForm(forms.ModelForm):
    """Experiment form with the champion picked from the configured models."""

    class Meta:  # pylint: disable=missing-class-docstring
        model = models.ArenaExperiment
        fields = (
            "name",
            "description",
            "is_active",
            "champion_model_hrid",
            "sampling_rate",
            "daily_cap_per_user",
            "min_votes_for_conclusion",
            "champion_input_price_eur_per_mtok",
            "champion_output_price_eur_per_mtok",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["champion_model_hrid"] = _model_choice_field(
            "Champion (production model)", with_provider_list=False
        )


class ArenaChallengerForm(forms.ModelForm):
    """Challenger form with a dropdown of configured models plus greyed provider models."""

    class Meta:  # pylint: disable=missing-class-docstring
        model = models.ArenaChallenger
        fields = (
            "experiment",
            "model_hrid",
            "input_price_eur_per_mtok",
            "output_price_eur_per_mtok",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["model_hrid"] = _model_choice_field("Challenger", with_provider_list=True)


class ArenaChallengerInline(admin.TabularInline):
    """Challengers of an experiment, edited on the experiment page."""

    model = models.ArenaChallenger
    form = ArenaChallengerForm
    extra = 1
    fields = ("model_hrid", "input_price_eur_per_mtok", "output_price_eur_per_mtok")


@admin.register(models.ArenaExperiment)
class ArenaExperimentAdmin(admin.ModelAdmin):
    """Blind champion-versus-challenger experiments, with a results page per experiment."""

    form = ArenaExperimentForm
    inlines = [ArenaChallengerInline]
    list_display = (
        "name",
        "is_active",
        "champion_model_hrid",
        "sampling_rate",
        "daily_cap_per_user",
        "challenger_list",
        "results_link",
    )
    list_filter = ("is_active",)
    fieldsets = (
        (None, {"fields": ("name", "description", "is_active")}),
        (
            "Sampling",
            {"fields": ("sampling_rate", "daily_cap_per_user", "min_votes_for_conclusion")},
        ),
        (
            "Champion",
            {
                "description": (
                    "The champion is the model conversations are pinned to (the production "
                    "model). It is on one side of every arena turn. The other side is ONE "
                    "challenger drawn at random from the list below; which side each model is "
                    "shown on is shuffled per turn. Conversations pinned to another model are "
                    "never compared. Prices feed the cost columns of the results page."
                ),
                "fields": (
                    "champion_model_hrid",
                    "champion_input_price_eur_per_mtok",
                    "champion_output_price_eur_per_mtok",
                ),
            },
        ),
    )

    @admin.display(description="Challengers")
    def challenger_list(self, obj):
        """Comma separated challenger hrids."""
        return ", ".join(obj.challengers.values_list("model_hrid", flat=True)) or "-"

    @admin.display(description="Results")
    def results_link(self, obj):
        """Link to the results page of the experiment."""
        url = reverse("admin:chat_arenaexperiment_results", args=[obj.pk])
        return format_html('<a href="{}">Results</a>', url)

    def get_urls(self):
        """Add the results page under the experiment's admin URLs."""
        custom = [
            path(
                "<uuid:object_id>/results/",
                self.admin_site.admin_view(self.results_view),
                name="chat_arenaexperiment_results",
            ),
        ]
        return custom + super().get_urls()

    def results_view(self, request, object_id):
        """Render win rates, intervals, costs and slices for one experiment.

        Gated by the dedicated ``view_arena_results`` permission so DINUM developers
        can read results without being superusers.
        """
        if not request.user.has_perm("chat.view_arena_results"):
            raise PermissionDenied
        experiment = get_object_or_404(models.ArenaExperiment, pk=object_id)
        include_seed = request.GET.get("seed", "1") != "0"
        results = build_results(experiment, include_seed=include_seed)
        context = {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "title": f"Arena results: {experiment.name}",
            "experiment": experiment,
            "results": results,
        }
        return render(request, "admin/chat/arenaexperiment/results.html", context)


@admin.register(models.ArenaComparison)
class ArenaComparisonAdmin(admin.ModelAdmin):
    """Read-only log of blind comparisons."""

    list_display = (
        "drawn_at",
        "experiment",
        "challenger_model_hrid",
        "champion_side",
        "status",
        "winner",
        "context_tags",
        "is_seed",
    )
    list_filter = ("experiment", "status", "winner", "challenger_model_hrid", "is_seed")
    date_hierarchy = "drawn_at"
    search_fields = ("conversation__id", "user__email")
    readonly_fields = [
        field.name
        for field in models.ArenaComparison._meta.fields  # noqa: SLF001 # pylint: disable=protected-access
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
