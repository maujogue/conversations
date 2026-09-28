import { UIMessage } from 'ai';
import { useCallback, useEffect, useState } from 'react';

import { RoutingEvent } from '@/features/chat/api/useChat';
import { useChatPreferencesStore } from '@/features/chat/stores/useChatPreferencesStore';
import { TierSlug, isTierSlug } from '@/features/chat/types';
import { getMessageRoutingMetadata } from '@/features/chat/utils/getMessageRouting';
import { useAnalytics } from '@/libs';

/** Ordinal weight of a tier; `auto` is not a tier the router can pick. */
const TIER_ORDER: Record<Exclude<TierSlug, 'auto'>, number> = {
  simple: 1,
  standard: 2,
  complex: 3,
};

/** Wasteful turns needed before the hint appears (spec 5.3). */
export const WASTEFUL_PIN_HINT_THRESHOLD = 3;

const tierWeight = (tier: string | undefined): number | undefined =>
  isTierSlug(tier) && tier !== 'auto' ? TIER_ORDER[tier] : undefined;

/**
 * A turn the user paid for: the tier was pinned by hand and the router, which
 * still classifies the question, would have picked a cheaper one.
 *
 * `router_would_pick` only reaches the frontend through the persisted message
 * metadata (and the end-of-turn annotation), never through the transient
 * `data-routing` part - so a turn whose decision is only known from the live
 * event is simply not counted.
 */
const isWastefulTurn = (
  message: UIMessage,
  liveEvent?: RoutingEvent,
): boolean => {
  if (message.role !== 'assistant') {
    return false;
  }
  const metadata = getMessageRoutingMetadata(message);
  const tierSource = liveEvent?.tier_source ?? metadata.tier_source;
  if (tierSource !== 'user') {
    return false;
  }
  const pinned = tierWeight(liveEvent?.tier ?? metadata.tier);
  const wouldPick = tierWeight(metadata.router_would_pick);
  if (pinned === undefined || wouldPick === undefined) {
    return false;
  }
  return wouldPick < pinned;
};

export const countWastefulTurns = (
  messages: UIMessage[],
  routingByMessageId: Record<string, RoutingEvent> = {},
): number =>
  messages.filter((message) =>
    isWastefulTurn(message, routingByMessageId[message.id]),
  ).length;

interface UseWastefulPinHintParams {
  conversationId: string | undefined;
  messages: UIMessage[];
  routingByMessageId?: Record<string, RoutingEvent>;
}

interface UseWastefulPinHintResult {
  /** Whether the one-line hint belongs under the last answer's caption. */
  showHint: boolean;
  /** The "Back to Auto" link: unpins the tier and counts the hint as followed. */
  returnToAuto: () => void;
}

/**
 * The wasteful-pin hint of spec 5.3: once three answers of this conversation
 * ran on a hand-pinned tier the router would have undercut, offer to go back
 * to Auto. Shown at most once per conversation, and latched for the rest of
 * the conversation so marking it shown does not make it vanish mid-read.
 */
export const useWastefulPinHint = ({
  conversationId,
  messages,
  routingByMessageId,
}: UseWastefulPinHintParams): UseWastefulPinHintResult => {
  const selectedTier = useChatPreferencesStore((state) => state.selectedTier);
  const setSelectedTier = useChatPreferencesStore(
    (state) => state.setSelectedTier,
  );
  const autoHintShownFor = useChatPreferencesStore(
    (state) => state.autoHintShownFor,
  );
  const markAutoHintShown = useChatPreferencesStore(
    (state) => state.markAutoHintShown,
  );
  const { trackEvent } = useAnalytics();
  const [shownFor, setShownFor] = useState<string | null>(null);

  const wastefulTurns = countWastefulTurns(messages, routingByMessageId);
  const alreadyShown = conversationId
    ? !!autoHintShownFor[conversationId]
    : true;
  const eligible =
    !!conversationId &&
    selectedTier !== 'auto' &&
    !alreadyShown &&
    wastefulTurns >= WASTEFUL_PIN_HINT_THRESHOLD;

  useEffect(() => {
    if (!eligible || !conversationId) {
      return;
    }
    setShownFor(conversationId);
    markAutoHintShown(conversationId);
    trackEvent({
      eventName: 'router_auto_hint_shown',
      properties: { tier: selectedTier },
    });
    // `selectedTier` is read for the event payload only: re-firing on a tier
    // change would double-count a hint that is already on screen.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eligible, conversationId, markAutoHintShown, trackEvent]);

  const returnToAuto = useCallback(() => {
    setSelectedTier('auto');
    setShownFor(null);
    trackEvent({ eventName: 'router_auto_hint_followed' });
  }, [setSelectedTier, trackEvent]);

  return {
    showHint:
      !!conversationId &&
      shownFor === conversationId &&
      selectedTier !== 'auto',
    returnToAuto,
  };
};
