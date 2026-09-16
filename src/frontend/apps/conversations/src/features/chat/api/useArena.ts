import { UIMessage } from 'ai';

import { APIError, errorCauses, fetchAPI } from '@/api';
import { ChatConversation } from '@/features/chat/types';

export type ArenaSide = 'left' | 'right';

/** What the user can vote: a displayed side, or a draw. */
export type ArenaVote = ArenaSide | 'tie' | 'both_bad';

export type ArenaDraw =
  | { arena: false }
  | { arena: true; comparison_id: string };

const NO_ARENA: ArenaDraw = { arena: false };

/**
 * Ask the backend whether the next turn is an arena turn. Any failure is
 * treated as "no arena": the message is then sent through the normal path.
 */
export const drawArena = async (
  conversationId: string,
  forceWebSearch: boolean,
  message?: UIMessage,
): Promise<ArenaDraw> => {
  try {
    const response = await fetchAPI(`chats/${conversationId}/arena/draw/`, {
      method: 'POST',
      body: JSON.stringify({ force_web_search: forceWebSearch, message }),
    });
    if (!response.ok) {
      return NO_ARENA;
    }
    const data = (await response.json()) as Partial<ArenaDraw> | null;
    if (
      data &&
      data.arena === true &&
      typeof (data as { comparison_id?: unknown }).comparison_id === 'string'
    ) {
      return data as ArenaDraw;
    }
    return NO_ARENA;
  } catch {
    return NO_ARENA;
  }
};

/** Milestones the backend flags on a user's vote count. */
export type ArenaMilestone = 'first_vote' | 'tenth_vote' | 'hundredth_vote';

/**
 * Figures returned with a vote, shown in the thanks card. Labels are i18n
 * keys (`router.tier.standard`, `router.task.writing`, ...), never model
 * names.
 */
export interface ArenaAcknowledgement {
  /** This user's votes on comparisons under 90 days. */
  user_votes: number;
  experiment_votes: number;
  tier_label: string | null;
  task_label: string | null;
  domain_label: string | null;
  milestone: ArenaMilestone | null;
}

export interface ArenaVoteResult {
  conversation: ChatConversation;
  /** `null` on abandonment, or when the backend sends none. */
  acknowledgement: ArenaAcknowledgement | null;
}

const isAcknowledgement = (value: unknown): value is ArenaAcknowledgement =>
  typeof value === 'object' &&
  value !== null &&
  typeof (value as ArenaAcknowledgement).user_votes === 'number' &&
  typeof (value as ArenaAcknowledgement).experiment_votes === 'number';

/**
 * Record the vote for a comparison: a side, `tie` (both good) or `both_bad`.
 * `side: null` abandons it (the backend keeps the production answer). Returns
 * the updated conversation and, for a real vote, the acknowledgement block.
 */
export const voteArena = async (
  conversationId: string,
  comparisonId: string,
  side: ArenaVote | null,
): Promise<ArenaVoteResult> => {
  const response = await fetchAPI(
    `chats/${conversationId}/arena/${comparisonId}/vote/`,
    {
      method: 'POST',
      body: JSON.stringify({ side }),
    },
  );
  if (!response.ok) {
    throw new APIError(
      'Failed to record the arena vote',
      await errorCauses(response),
    );
  }
  const { acknowledgement, ...conversation } =
    (await response.json()) as ChatConversation & { acknowledgement?: unknown };
  return {
    conversation,
    acknowledgement: isAcknowledgement(acknowledgement)
      ? acknowledgement
      : null,
  };
};

/** Machine-readable reasons the backend refuses a second opinion (spec 8.2). */
export type ArenaManualErrorCode =
  | 'arena_manual_no_answer'
  | 'arena_manual_not_last_message'
  | 'arena_manual_unavailable'
  | 'arena_manual_exhausted'
  /** Anything else: flag off, foreign conversation, network, bad payload. */
  | 'arena_manual_failed';

const MANUAL_ERROR_CODES: ArenaManualErrorCode[] = [
  'arena_manual_no_answer',
  'arena_manual_not_last_message',
  'arena_manual_unavailable',
  'arena_manual_exhausted',
];

/** A refused second opinion, carrying the code the caller branches on. */
export class ArenaManualError extends Error {
  readonly code: ArenaManualErrorCode;

  constructor(code: ArenaManualErrorCode) {
    super(`Second opinion refused: ${code}`);
    this.name = 'ArenaManualError';
    this.code = code;
  }
}

export interface ArenaManualComparison {
  comparison_id: string;
  /** The side the client must stream: the champion's is already committed. */
  side: ArenaSide;
}

/**
 * Ask for a second opinion on `messageId`, the last assistant answer of the
 * conversation (router spec 8.2). The answer already on screen becomes one
 * side of a new comparison; only the returned `side` has to be streamed.
 */
export const requestManualArena = async (
  conversationId: string,
  messageId: string,
): Promise<ArenaManualComparison> => {
  const response = await fetchAPI(`chats/${conversationId}/arena/manual/`, {
    method: 'POST',
    body: JSON.stringify({ message_id: messageId }),
  });
  if (!response.ok) {
    let code: ArenaManualErrorCode = 'arena_manual_failed';
    if (response.status === 409) {
      try {
        const data = (await response.json()) as { error?: unknown } | null;
        const reported = data?.error;
        if (
          typeof reported === 'string' &&
          (MANUAL_ERROR_CODES as string[]).includes(reported)
        ) {
          code = reported as ArenaManualErrorCode;
        }
      } catch {
        // Not JSON: keep the generic code.
      }
    }
    throw new ArenaManualError(code);
  }
  const data = (await response.json()) as Partial<ArenaManualComparison> | null;
  if (
    typeof data?.comparison_id !== 'string' ||
    (data.side !== 'left' && data.side !== 'right')
  ) {
    throw new ArenaManualError('arena_manual_failed');
  }
  return { comparison_id: data.comparison_id, side: data.side };
};
