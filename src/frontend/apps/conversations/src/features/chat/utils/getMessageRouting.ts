import { UIMessage } from 'ai';

import { RoutingEvent, isRoutingEvent } from '@/features/chat/api/useChat';

/**
 * Router fields persisted with an assistant message (spec 5.5 / 9), next to
 * `co2_impact`. All optional: older messages and non-routed turns carry none.
 */
export interface RoutingMetadata {
  tier?: string;
  tier_source?: string;
  /**
   * The tier the classifier picked before a pinned tier was applied (spec
   * 5.5). Only present on the persisted metadata / the end-of-turn annotation,
   * never on the `data-routing` part.
   */
  router_would_pick?: string;
  domain?: string;
  task?: string;
  reasoning_effort?: string;
  /** Wall-clock seconds the model spent reasoning before its first token. */
  reasoning_seconds?: number;
  co2_source?: 'provider' | 'estimated' | 'estimated_reasoning';
}

export const getMessageRoutingMetadata = (
  message: UIMessage,
): RoutingMetadata => {
  const metadata = message.metadata;
  if (typeof metadata !== 'object' || metadata === null) {
    return {};
  }
  return metadata;
};

/**
 * The routing decision to show above an assistant message: the live stream
 * event when the turn was streamed in this session, otherwise the persisted
 * metadata (after a reload). `undefined` when the turn was not routed.
 */
export const getMessageRouting = (
  message: UIMessage,
  liveEvent?: RoutingEvent,
): RoutingEvent | undefined => {
  if (liveEvent) {
    return liveEvent;
  }
  const { tier, tier_source } = getMessageRoutingMetadata(message);
  const candidate = { tier, tier_source };
  return isRoutingEvent(candidate) ? candidate : undefined;
};

export const getMessageReasoningSeconds = (
  message: UIMessage,
): number | undefined => {
  const seconds = getMessageRoutingMetadata(message).reasoning_seconds;
  return typeof seconds === 'number' && seconds >= 0 ? seconds : undefined;
};
