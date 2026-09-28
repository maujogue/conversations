"""Chat conversation collection index state enums declaration."""

from enum import StrEnum


class CollectionIndexState(StrEnum):
    """Defines the possible index states for a conversation's collection."""

    UNINDEXED = "unindexed"  # No collection; default for all new conversations
    DEINDEXED = "deindexed"  # Was indexed, then de-indexed by the inactivity command
    INDEXING = "indexing"  # Claim held, reindex in progress
    INDEXED = "indexed"  # Collection exists; collection_id holds real backend ID
    ERROR = "error"  # Last attempt failed; will retry on next request

    @classmethod
    def choices(cls):
        """Return a list of tuples for each enum member."""
        return [(member.value, member.name) for member in cls]


class AttachmentIndexState(StrEnum):
    """Per-attachment RAG indexing lifecycle (project attachments).

    Distinct from the conversation-level `CollectionIndexState`: this tracks a
    single file's journey into the RAG backend, set by the project indexing
    task. `FAILED` is terminal until a manual re-index; `processing_error` on
    the attachment carries the reason.
    """

    NOT_INDEXED = "not_indexed"  # Default; not yet sent to the backend
    INDEXING = "indexing"  # Indexing task is running
    INDEXED = "indexed"  # Stored in the backend; rag_document_id is set
    FAILED = "failed"  # Last indexing attempt failed; see processing_error

    @classmethod
    def choices(cls):
        """Return a list of tuples for each enum member."""
        return [(member.value, member.name) for member in cls]


class ArenaComparisonStatus(StrEnum):
    """Lifecycle of one blind comparison between the champion and a challenger."""

    PENDING = "pending"  # Drawn, at least one side still streaming or not voted yet
    VOTED = "voted"  # The user picked a side; the winner is in the conversation
    ABANDONED = "abandoned"  # No vote; the champion answer was kept
    ERRORED = "errored"  # One side failed; the surviving answer (if any) was kept

    @classmethod
    def choices(cls):
        """Return a list of tuples for each enum member."""
        return [(member.value, member.name) for member in cls]


class ArenaSide(StrEnum):
    """Which column an answer was displayed in. Randomized per comparison."""

    LEFT = "left"
    RIGHT = "right"

    @classmethod
    def choices(cls):
        """Return a list of tuples for each enum member."""
        return [(member.value, member.name) for member in cls]


class ArenaRole(StrEnum):
    """Which model produced an answer, independently of the side it was shown on."""

    CHAMPION = "champion"
    CHALLENGER = "challenger"

    @classmethod
    def choices(cls):
        """Return a list of tuples for each enum member."""
        return [(member.value, member.name) for member in cls]


class ArenaVoteOutcome(StrEnum):
    """What the user's vote said. The first two are the roles, the others are draws."""

    CHAMPION = ArenaRole.CHAMPION.value
    CHALLENGER = ArenaRole.CHALLENGER.value
    TIE = "tie"  # Both answers were judged good
    BOTH_BAD = "both_bad"  # Neither answer was judged good

    @classmethod
    def choices(cls):
        """Return a list of tuples for each enum member."""
        return [(member.value, member.name) for member in cls]


class ArenaContextTag(StrEnum):
    """Context a comparison turn ran with. A turn can carry several tags."""

    PLAIN = "plain"
    WEB_SEARCH = "web_search"
    ATTACHMENT = "attachment"
    PROJECT = "project"


class ArenaOrigin(StrEnum):
    """How a comparison came to exist (router spec 8.1 and 8.2)."""

    DRAW = "draw"  # Sampled by the arena on an eligible turn
    MANUAL = "manual"  # Asked for by the user with the second-opinion button

    @classmethod
    def choices(cls):
        """Return a list of tuples for each enum member."""
        return [(member.value, member.name) for member in cls]


class RoutingTier(StrEnum):
    """Complexity tier a turn is routed to. Ordinal: simple < standard < complex."""

    SIMPLE = "simple"
    STANDARD = "standard"
    COMPLEX = "complex"

    @classmethod
    def choices(cls):
        """Return a list of tuples for each enum member."""
        return [(member.value, member.name) for member in cls]


class RoutingDomain(StrEnum):
    """Domain tag produced by the router classifier (spec 4.1)."""

    GENERAL = "general"
    ADMINISTRATIVE = "administrative"
    LEGAL = "legal"
    HEALTH = "health"
    FINANCE = "finance"
    HR = "hr"
    IT_SOFTWARE = "it_software"
    SCIENCE_EDUCATION = "science_education"
    COMMUNICATION = "communication"
    DEFENSE_SECURITY = "defense_security"
    ENVIRONMENT = "environment"
    CULTURE_SOCIETY = "culture_society"

    @classmethod
    def choices(cls):
        """Return a list of tuples for each enum member."""
        return [(member.value, member.name) for member in cls]


class RoutingTask(StrEnum):
    """Task tag produced by the router classifier (spec 4.2)."""

    QA_KNOWLEDGE = "qa_knowledge"
    WRITING = "writing"
    SUMMARIZATION = "summarization"
    TRANSLATION = "translation"
    CODING = "coding"
    DATA_ANALYSIS = "data_analysis"
    DOCUMENT_QA = "document_qa"
    RESEARCH = "research"
    REASONING = "reasoning"
    BRAINSTORM_CREATIVE = "brainstorm_creative"
    CLASSIFICATION_EXTRACTION = "classification_extraction"
    CONVERSATION = "conversation"

    @classmethod
    def choices(cls):
        """Return a list of tuples for each enum member."""
        return [(member.value, member.name) for member in cls]


class RoutingReason(StrEnum):
    """Why the router landed on a tier (spec 5.5, `router_reason`)."""

    CLASSIFIED = "classified"  # The classifier ran and its complexity was used
    SHORTCUT = "shortcut"  # The classifier call was skipped (short follow-up)
    CONSTRAINT = "constraint"  # A capability constraint raised the tier
    CONSTRAINT_FALLBACK = "constraint_fallback"  # No tier model fits; default model used
    FALLBACK = "fallback"  # Classifier timeout or error
    USER_PINNED = "user_pinned"  # The user pinned a tier on the conversation

    @classmethod
    def choices(cls):
        """Return a list of tuples for each enum member."""
        return [(member.value, member.name) for member in cls]


class TierSource(StrEnum):
    """Who decided the tier for a turn (spec 5.5, `tier_source`)."""

    ROUTER = "router"
    USER = "user"
    CONSTRAINT = "constraint"

    @classmethod
    def choices(cls):
        """Return a list of tuples for each enum member."""
        return [(member.value, member.name) for member in cls]


class ReasoningEffort(StrEnum):
    """Reasoning effort requested from a reasoning-capable model (spec 3.2)."""

    MEDIUM = "medium"
    HIGH = "high"

    @classmethod
    def choices(cls):
        """Return a list of tuples for each enum member."""
        return [(member.value, member.name) for member in cls]
