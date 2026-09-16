"""Arena endpoints: draw a blind comparison for the next turn, then vote on it.

Both actions live on the conversation viewset. The streaming endpoint itself
learns about arena mode through two query parameters (``arena_comparison`` and
``arena_side``) handled in ``ChatViewSet.post_conversation``. No response of
these endpoints ever carries a model name: which model sits on which side is a
server-side fact.
"""

import logging

from drf_spectacular.utils import extend_schema
from rest_framework import decorators, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response

from core.feature_flags.helpers import is_feature_enabled

from chat import arena as arena_service
from chat import models, serializers

logger = logging.getLogger(__name__)


class ArenaMixin:
    """Arena actions for ``ChatViewSet``."""

    def _get_pending_comparison_or_404(self, conversation, comparison_id):
        """The pending comparison of this conversation and user, or 404."""
        comparison = models.ArenaComparison.objects.filter(
            pk=comparison_id,
            conversation=conversation,
            user=self.request.user,
        ).first()
        if comparison is None:
            raise NotFound("Unknown arena comparison for this conversation.")
        return comparison

    @decorators.action(
        methods=["post"],
        detail=True,
        url_path="arena/draw",
        url_name="arena-draw",
    )
    def post_arena_draw(self, request, pk):  # pylint: disable=unused-argument
        """Decide whether the next turn of this conversation is an arena turn.

        Returns ``{"arena": false}`` for a normal turn, otherwise the id of the
        pending comparison the client must pass to two streaming requests, one
        per side.
        """
        conversation = self.get_object()
        serializer = serializers.ArenaDrawSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        force_web_search = serializer.validated_data["force_web_search"]
        message = serializer.validated_data.get("message")

        # The experiment is chosen by the turn's tier, so the turn is routed here and
        # the decision travels to the comparison. The streaming request that follows
        # runs in arena mode and does not route again.
        routing_decision = None
        if message is not None and is_feature_enabled(request.user, "router"):
            routing_decision = self._route_turn(
                conversation,
                message,
                force_web_search=force_web_search,
                requested_model_hrid=None,
            )

        comparison = arena_service.draw_comparison(
            conversation=conversation,
            user=request.user,
            force_web_search=force_web_search,
            last_message=message,
            routing_decision=routing_decision,
        )
        if comparison is None:
            return Response({"arena": False}, status=status.HTTP_200_OK)
        return Response({"arena": True, "comparison_id": str(comparison.pk)})

    @extend_schema(
        request=serializers.ArenaManualSerializer,
        responses={200: serializers.ArenaManualResponseSerializer},
    )
    @decorators.action(
        methods=["post"],
        detail=True,
        url_path="arena/manual",
        url_name="arena-manual",
    )
    def post_arena_manual(self, request, pk):  # pylint: disable=unused-argument
        """Run a second opinion on the last answer (router spec 8.2).

        The answer already in the conversation becomes the champion side of a new
        ``origin=manual`` comparison, so the client only streams the challenger side:
        the returned ``side`` is the column it must fill. No daily cap; the per-user
        stream concurrency limit and the chat cooldown are the only guards.
        """
        if not is_feature_enabled(request.user, "arena_manual"):
            raise PermissionDenied("Second opinions are not enabled.")
        conversation = self.get_object()
        serializer = serializers.ArenaManualSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            comparison = arena_service.create_manual_comparison(
                conversation=conversation,
                user=request.user,
                message_id=serializer.validated_data["message_id"],
            )
        except arena_service.ArenaConflict as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        return Response(
            {"comparison_id": str(comparison.pk), "side": comparison.challenger_side},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=serializers.ArenaVoteSerializer,
        responses={200: serializers.ArenaVoteResponseSerializer},
    )
    @decorators.action(
        methods=["post"],
        detail=True,
        url_path=r"arena/(?P<comparison_id>[0-9a-f-]{36})/vote",
        url_name="arena-vote",
    )
    def post_arena_vote(self, request, pk, comparison_id):  # pylint: disable=unused-argument
        """Record the user's pick (or abandonment) and commit the chosen answer.

        Returns the updated conversation so the client can replace the split view
        with the committed history in one round trip, plus an ``acknowledgement``
        block (vote counts, routing labels, milestone) on a vote or a draw, and
        ``null`` on an abandonment.
        """
        conversation = self.get_object()
        serializer = serializers.ArenaVoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comparison = self._get_pending_comparison_or_404(conversation, comparison_id)

        try:
            comparison = arena_service.vote(comparison, serializer.validated_data["side"])
        except arena_service.ArenaConflict as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        conversation.refresh_from_db()
        return Response(
            {
                **self.get_serializer(conversation).data,
                "acknowledgement": arena_service.build_acknowledgement(comparison, request.user),
            },
            status=status.HTTP_200_OK,
        )

    def _resolve_arena_stream_params(self, conversation, validated_query_params, message):
        """Turn the arena query parameters into ``(comparison, role, model_hrid)``.

        Raises ``NotFound`` when the comparison does not belong to this conversation
        and user, ``ValidationError`` when it is closed or the side already ran.
        """
        comparison_id = validated_query_params.get("arena_comparison")
        side = validated_query_params.get("arena_side")
        if not comparison_id:
            return None
        comparison = self._get_pending_comparison_or_404(conversation, comparison_id)
        role = comparison.role_for_side(side)
        try:
            comparison = arena_service.claim_candidate(comparison, role, message)
        except arena_service.ArenaConflict as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
        return comparison, role, comparison.model_hrid_for_side(side)
